"""Persist completed CLI turns as structured, transactional SQLite sessions."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .events import history_from_entries, validate_history


SCHEMA_VERSION = 1
SCHEMA = (
    """CREATE TABLE sessions (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        reason TEXT NOT NULL, last_log TEXT NOT NULL)""",
    """CREATE TABLE messages (
        session_id TEXT NOT NULL REFERENCES sessions(id), position INTEGER NOT NULL,
        role TEXT NOT NULL, content TEXT, tool_calls TEXT, tool_call_id TEXT,
        PRIMARY KEY (session_id, position))""",
    """CREATE TABLE tool_runs (
        session_id TEXT NOT NULL REFERENCES sessions(id), run_log TEXT NOT NULL,
        step INTEGER NOT NULL, call_id TEXT NOT NULL, name TEXT NOT NULL,
        arguments TEXT NOT NULL, approved INTEGER, approval_source TEXT, result TEXT,
        PRIMARY KEY (session_id, run_log, step, call_id))""",
)


@dataclass(frozen=True)
class Session:
    id: str
    workspace: str
    created_at: str
    updated_at: str
    reason: str
    last_log: str


@dataclass(frozen=True)
class Message:
    position: int
    role: str
    content: str | None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None

    def to_api(self) -> dict:
        message = {"role": self.role, "content": self.content}
        if self.tool_calls is not None:
            message["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            message["tool_call_id"] = self.tool_call_id
        return message


@dataclass(frozen=True)
class ToolRun:
    run_log: str
    step: int
    call_id: str
    name: str
    arguments: str
    approved: bool | None = None
    approval_source: str | None = None
    result: dict | None = None


def project_run(entries: list[dict]) -> tuple[list[dict], list[ToolRun]]:
    """Extract the latest transcript and this run's actual tool records."""
    messages, calls = [], {}
    run_log = entries[0]["log_path"]
    for entry in entries:
        event = entry["event"]
        if event == "model_request":
            messages = list(entry["payload"]["messages"][1:])
        elif event == "model_response":
            message = entry["response"]["choices"][0]["message"]
            messages.append({"role": "assistant", "content": message.get("content"),
                             **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {})})
            for call in message.get("tool_calls") or []:
                function = call.get("function", {})
                # Invalid requests remain in messages, even if they have no valid ToolRun key.
                if all(isinstance(value, str) for value in (call.get("id"), function.get("name"), function.get("arguments"))):
                    key = (entry["step"], call["id"])
                    calls[key] = {"run_log": run_log, "step": entry["step"], "call_id": call["id"],
                                  "name": function["name"], "arguments": function["arguments"]}
        elif event in ("tool_approval", "tool_result"):
            key = (entry["step"], entry["tool_call_id"])
            if key not in calls:
                raise ValueError("工具记录缺少对应的模型请求，未保存会话。")
            if event == "tool_approval":
                calls[key].update(approved=entry["approved"], approval_source=entry["source"])
            else:
                calls[key]["result"] = entry["result"]
                messages.append({"role": "tool", "tool_call_id": entry["tool_call_id"],
                                 "content": json.dumps(entry["result"], ensure_ascii=False)})
    return messages, [ToolRun(**call) for call in calls.values()]


class SessionStore:
    def __init__(self, path: Path):
        self.path = path.resolve()

    @contextmanager
    def connect(self, create: bool = False):
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        target = str(self.path) if create else self.path.as_uri() + "?mode=ro"
        database = sqlite3.connect(target, uri=not create)
        database.row_factory = sqlite3.Row
        try:
            database.execute("PRAGMA foreign_keys = ON")
            version = database.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and create:
                if database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError("数据库没有 learn-agent 版本标记，未修改原数据库。")
                with database:
                    database.execute("BEGIN IMMEDIATE")
                    for statement in SCHEMA:
                        database.execute(statement)
                    database.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise ValueError(f"不支持的会话数据库版本 {version}；当前支持 {SCHEMA_VERSION}，未自动迁移。")
            yield database
        finally:
            database.close()

    def initialize(self) -> None:
        with self.connect(create=True):
            pass

    def list_sessions(self) -> list[Session]:
        if not self.path.exists():
            return []
        with self.connect() as database:
            return [Session(**dict(row)) for row in database.execute("SELECT * FROM sessions ORDER BY updated_at DESC")]

    def read(self, session_id: str) -> tuple[Session, list[Message], list[ToolRun]]:
        if not self.path.exists():
            raise ValueError("尚无会话数据库，请先完成一次普通任务。")
        with self.connect() as database, database:
            database.execute("BEGIN")
            row = database.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                raise ValueError(f"找不到会话 {session_id}。")
            messages = []
            for row_message in database.execute(
                    "SELECT position, role, content, tool_calls, tool_call_id FROM messages WHERE session_id = ? ORDER BY position", (session_id,)):
                values = dict(row_message)
                values["tool_calls"] = json.loads(values["tool_calls"]) if values["tool_calls"] is not None else None
                messages.append(Message(**values))
            tools = []
            for row_tool in database.execute(
                    "SELECT run_log, step, call_id, name, arguments, approved, approval_source, result FROM tool_runs WHERE session_id = ? ORDER BY rowid", (session_id,)):
                values = dict(row_tool)
                values["approved"] = bool(values["approved"]) if values["approved"] is not None else None
                values["result"] = json.loads(values["result"]) if values["result"] is not None else None
                tools.append(ToolRun(**values))
            return Session(**dict(row)), messages, tools

    def restore(self, session_id: str, workspace: Path) -> tuple[Session, list[dict]]:
        session, messages, _ = self.read(session_id)
        if Path(session.workspace).resolve() != workspace.resolve():
            raise ValueError("会话属于另一个项目，请用 --workspace 指定原项目目录。")
        if session.reason not in ("stop", "max_model_calls"):
            raise ValueError("目前只支持继续正常结束或因模型请求次数上限停止的会话。")
        history = [message.to_api() for message in messages]
        validate_history(history)
        return session, history

    def save_run(self, session_id: str, log_path: Path, expected_log: str | None = None) -> None:
        entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not entries or entries[0]["event"] != "run_start" or entries[-1]["event"] != "run_end":
            raise ValueError("运行日志不完整，未保存会话；JSONL 保留供核对。")
        workspace = str(Path(entries[0]["workspace"]).resolve())
        run_log = str(log_path.resolve())
        messages, tools = project_run([{**entries[0], "log_path": run_log}, *entries[1:]])
        reason = entries[-1]["reason"]
        if reason in ("stop", "max_model_calls"):
            messages = history_from_entries(entries, Path(workspace))
        with self.connect(create=True) as database, database:
            database.execute("BEGIN IMMEDIATE")
            old = database.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if old is None:
                if expected_log is not None:
                    raise ValueError("原会话已不存在，未覆盖数据库。")
                database.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", (
                    session_id, workspace, entries[0]["time"], entries[-1]["time"], reason, run_log))
            else:
                if old["workspace"] != workspace or old["last_log"] != expected_log:
                    raise ValueError("会话已被其他运行更新或属于另一项目；本次 JSONL 已保留，未覆盖原会话。")
                database.execute("UPDATE sessions SET updated_at = ?, reason = ?, last_log = ? WHERE id = ?", (
                    entries[-1]["time"], reason, run_log, session_id))
                database.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            for position, message in enumerate(messages):
                database.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)", (
                    session_id, position, message["role"], message.get("content"),
                    json.dumps(message["tool_calls"], ensure_ascii=False) if message.get("tool_calls") else None,
                    message.get("tool_call_id")))
            for tool in tools:
                database.execute("INSERT INTO tool_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                    session_id, tool.run_log, tool.step, tool.call_id, tool.name, tool.arguments,
                    tool.approved, tool.approval_source,
                    json.dumps(tool.result, ensure_ascii=False) if tool.result is not None else None))
