"""One local HTTP task at a time, using the existing synchronous Agent core."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from uuid import uuid4

from fastapi import HTTPException

from . import checkpoints, loop, permissions
from .budget import RunBudget
from .events import EventRecorder, record_event
from .prompts import task_messages
from .providers import DeepSeekProvider, ModelProvider
from .sessions import SessionStore


# Workspace and checkpoint selection are process-global in the current core.
CORE_LOCK = Lock()


@dataclass
class ApiTask:
    id: str
    session_id: str
    log_path: Path
    events: list[dict] = field(default_factory=list)
    pending_approval: dict | None = None
    approval_answer: bool | None = None
    cancel_requested: Event = field(default_factory=Event)
    finished: bool = False
    session_saved: bool = False
    error_type: str | None = None
    persistence_error: str | None = None
    thread: Thread | None = None

    def snapshot(self) -> dict:
        last = self.events[-1] if self.events else {}
        ended = last.get("event") == "run_end"
        final = next((e["response"]["choices"][0]["message"].get("content") for e in reversed(self.events)
                      if e["event"] == "model_response" and e["response"]["choices"][0]["finish_reason"] == "stop"), None)
        return deepcopy({
            "id": self.id, "session_id": self.session_id,
            "state": last.get("task_state", "failed" if self.error_type else "running"),
            "reason": last.get("reason") if ended else ("error" if self.error_type else None),
            "finished": self.finished, "cancel_requested": self.cancel_requested.is_set(),
            "pending_approval": self.pending_approval, "events": self.events,
            "final_answer": final if ended and last["reason"] == "stop" else None,
            "summary": last.get("summary") if ended else None,
            "session_saved": self.session_saved, "error_type": self.error_type,
            "persistence_error": self.persistence_error, "log_path": str(self.log_path),
        })


class TaskManager:
    def __init__(self, workspace: Path, log_dir: Path, api_key: str, provider: ModelProvider | None = None):
        self.workspace = workspace.resolve()
        if not self.workspace.is_dir():
            raise ValueError("工作目录不存在或不是目录。")
        self.log_dir = log_dir.resolve()
        self.store = SessionStore(self.log_dir / "sessions.sqlite3")
        self.store.initialize()
        self.api_key = api_key
        self.provider = provider if provider is not None else DeepSeekProvider()
        self.condition = Condition()
        self.new_sessions: dict[str, dict] = {}
        self.tasks: dict[str, ApiTask] = {}
        self.closed = False

    def create_session(self) -> dict:
        with self.condition:
            if self.closed:
                raise HTTPException(409, "服务正在关闭。")
            session = {"id": uuid4().hex, "workspace": str(self.workspace),
                       "created_at": datetime.now(timezone.utc).isoformat(), "reason": None}
            self.new_sessions[session["id"]] = session
            return dict(session)

    def list_sessions(self) -> list[dict]:
        with self.condition:
            sessions = dict(self.new_sessions)
            sessions.update({s.id: asdict(s) for s in self.store.list_sessions() if Path(s.workspace) == self.workspace})
            return sorted(sessions.values(), key=lambda s: s.get("updated_at", s["created_at"]), reverse=True)

    def read_session(self, session_id: str) -> dict:
        with self.condition:
            session = next((s for s in self.store.list_sessions() if s.id == session_id), None)
            if session is None:
                if session_id not in self.new_sessions:
                    raise HTTPException(404, "找不到此会话。") from None
                return {**self.new_sessions[session_id], "messages": [], "tool_runs": []}
            if Path(session.workspace) != self.workspace:
                raise HTTPException(404, "找不到当前项目的此会话。")
            session, messages, tools = self.store.read(session_id)
            return {**asdict(session), "messages": [asdict(m) for m in messages], "tool_runs": [asdict(t) for t in tools]}

    def submit(self, session_id: str, options: dict) -> dict:
        with self.condition:
            if self.closed:
                raise HTTPException(409, "服务正在关闭。")
            session = self.read_session(session_id)
            if not CORE_LOCK.acquire(blocking=False):
                raise HTTPException(409, "已有任务正在运行或等待确认；请先完成或取消该任务。")
            try:
                history, expected_log = [], None
                if session.get("last_log"):
                    try:
                        saved, history = self.store.restore(session_id, self.workspace)
                        expected_log = saved.last_log
                    except ValueError as error:
                        raise HTTPException(409, str(error)) from None
                task = ApiTask(uuid4().hex, session_id, self.log_dir / f"api-run-{uuid4().hex}.jsonl")
                task.thread = Thread(target=self._run, args=(task, options, history, expected_log), name=f"agent-{task.id}")
                self.tasks[task.id] = task
                task.thread.start()
            except BaseException:
                CORE_LOCK.release()
                raise
            return task.snapshot()

    def _task(self, task_id: str) -> ApiTask:
        if task_id not in self.tasks:
            raise HTTPException(404, "找不到本次服务中的此任务。")
        return self.tasks[task_id]

    def snapshot(self, task_id: str) -> dict:
        with self.condition:
            return self._task(task_id).snapshot()

    def approve(self, task_id: str, approval_id: str, approved: bool) -> dict:
        with self.condition:
            task = self._task(task_id)
            pending = task.pending_approval
            if task.finished or task.cancel_requested.is_set() or pending is None or pending["id"] != approval_id:
                raise HTTPException(409, "审批已失效、已处理或不属于此任务；请重新查询任务。")
            task.approval_answer = approved
            task.pending_approval = None
            self.condition.notify_all()
            return {"accepted": True, "task_id": task_id, "approval_id": approval_id, "approved": approved}

    def cancel(self, task_id: str) -> dict:
        with self.condition:
            task = self._task(task_id)
            if task.finished or (task.events and task.events[-1]["event"] == "run_end"):
                raise HTTPException(409, "任务已经结束。")
            task.cancel_requested.set()
            task.pending_approval = None
            self.condition.notify_all()
            return {"accepted": True, "task_id": task_id, "cancel_requested": True}

    def _request_approval(self, task: ApiTask, call_id: str, name: str, arguments: dict, action: str) -> bool:
        with self.condition:
            if task.cancel_requested.is_set():
                raise KeyboardInterrupt
            task.approval_answer = None
            task.pending_approval = {"id": uuid4().hex, "tool_call_id": call_id,
                                     "tool": name, "arguments": deepcopy(arguments), "action": action}
            self.condition.notify_all()
            self.condition.wait_for(lambda: task.approval_answer is not None or task.cancel_requested.is_set())
            task.pending_approval = None
            if task.cancel_requested.is_set():
                raise KeyboardInterrupt
            return task.approval_answer

    def _run(self, task: ApiTask, options: dict, history: list[dict], expected_log: str | None) -> None:
        budget = RunBudget(options["max_model_calls"], options["max_total_tokens"])

        def observe(entry):
            with self.condition:
                task.events.append(deepcopy(entry))
                self.condition.notify_all()

        try:
            with permissions.use_workspace(self.workspace):
                with task.log_path.open("x", encoding="utf-8") as stream:
                    events = EventRecorder(stream, on_event=observe)
                    payload = self.provider.build_request(task_messages(options["prompt"], self.workspace, history))
                    record_event(events, "run_start", session_id=task.session_id, task_id=task.id, transport="http",
                                 provider=self.provider.name, workspace=str(self.workspace), recovery_version=1,
                                 auto_read=options["auto_read"], denied_tools=options["denied_tools"],
                                 max_model_calls=budget.max_model_calls, max_total_tokens=budget.max_total_tokens,
                                 checkpoint=str(task.log_path.with_suffix(".checkpoint.json")), initial_messages=payload["messages"])
                    reason = "error"
                    try:
                        with checkpoints.track_changes(task.log_path):
                            reason = loop.run_agent(
                                payload, self.api_key, events, budget=budget, provider=self.provider,
                                auto_read=options["auto_read"], denied_tools=tuple(options["denied_tools"]),
                                request_approval=lambda *args: self._request_approval(task, *args),
                                is_cancelled=task.cancel_requested.is_set)
                    except KeyboardInterrupt:
                        reason = "cancelled"
                    except Exception as error:
                        task.error_type = type(error).__name__
                    finally:
                        record_event(events, "run_end", reason=reason, summary=budget.summary(),
                                     **({"error_type": task.error_type} if task.error_type else {}))
                try:
                    self.store.save_run(task.session_id, task.log_path, expected_log=expected_log)
                    task.session_saved = True
                except Exception as error:
                    task.persistence_error = type(error).__name__
        except Exception as error:
            task.error_type = type(error).__name__
        finally:
            with self.condition:
                task.pending_approval = None
                task.finished = True
                CORE_LOCK.release()
                self.condition.notify_all()

    def close(self) -> None:
        with self.condition:
            self.closed = True
            active = [task for task in self.tasks.values() if not task.finished]
            for task in active:
                task.cancel_requested.set()
                task.pending_approval = None
            self.condition.notify_all()
        for task in active:
            task.thread.join()
