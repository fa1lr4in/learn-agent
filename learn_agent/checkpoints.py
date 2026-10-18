"""Record file-tool changes and undo only unchanged, owned file versions."""

import base64
import difflib
import json
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import permissions


class CheckpointConflict(OSError):
    """The current file cannot be safely attributed to this checkpoint."""


def encode(content: bytes | None) -> str | None:
    return base64.b64encode(content).decode("ascii") if content is not None else None


def decode(content: str | None) -> bytes | None:
    return base64.b64decode(content, validate=True) if content is not None else None


def fingerprint(info: os.stat_result) -> dict:
    return {name: getattr(info, "st_" + name) for name in ("mtime_ns", "mode", "size", "ino", "dev")}


def snapshot(target: Path) -> tuple[bytes | None, dict | None]:
    try:
        before = target.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(before.st_mode):
        raise CheckpointConflict("目标不是普通文件，未操作。")
    content = target.read_bytes()
    after = target.lstat()
    if fingerprint(before) != fingerprint(after):
        raise CheckpointConflict("读取检查点快照期间文件已变化，未操作。")
    return content, fingerprint(after)


def git_status(root: Path) -> str | None:
    """Inspect only this project, without refreshing the Git index or running fsmonitor."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.quotepath=false",
             "-C", str(root), "status", "--short", "--untracked-files=normal", "--", "."],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            env={key: value for key, value in os.environ.items() if key.upper() != "DEEPSEEK_API_KEY"},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def file_diff(path: str, before: bytes | None, after: bytes | None) -> str:
    try:
        old = (before or b"").decode("utf-8")
        new = (after or b"").decode("utf-8")
    except UnicodeError:
        return f"二进制内容变化：原 {len(before or b'')} 字节，新 {len(after or b'')} 字节。\n"
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                 fromfile=f"a/{path}" if before is not None else "/dev/null",
                                 tofile=f"b/{path}" if after is not None else "/dev/null")
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


class Checkpoint:
    def __init__(self, log_path: Path, data: dict):
        self.log_path = log_path.resolve()
        self.path = self.log_path.with_suffix(".checkpoint.json")
        self.data = data

    def save(self) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def target(self, path: str) -> Path:
        root = permissions.WORKSPACE.resolve()
        if self.data["workspace"] != str(root):
            raise CheckpointConflict("检查点属于其他项目，请用 --workspace 指定原项目。")
        target = permissions.resolve_write_path(path)
        if target in (self.log_path, self.path):
            raise CheckpointConflict("不能通过文件工具修改本次运行的日志或检查点。")
        return target

    def change(self, target: Path, updated: bytes, operation):
        key = target.relative_to(permissions.WORKSPACE.resolve()).as_posix()
        if self.target(key) != target:
            raise CheckpointConflict("写入目标在路径解析后已变化，未操作。")
        before, stamp = snapshot(target)
        previous = self.data["files"].get(key)
        if previous is not None and (previous["state"] != "recorded" or
                                     (before, stamp) != (decode(previous["after"]), previous["fingerprint"])):
            raise CheckpointConflict("文件在本次追踪后又发生变化，未写入；请检查其他操作，不要强制覆盖。")
        entry = {"before": previous["before"] if previous else encode(before),
                 "after": encode(updated), "fingerprint": None, "state": "pending"}
        self.data["files"][key] = entry
        try:
            self.save()
        except BaseException:
            if previous is None:
                del self.data["files"][key]
            else:
                self.data["files"][key] = previous
            raise
        if snapshot(self.target(key)) != (before, stamp):
            raise CheckpointConflict("保存检查点期间文件已变化，未写入。")
        try:
            result = operation()
        except BaseException:
            if snapshot(self.target(key)) == (before, stamp):
                if previous is None:
                    del self.data["files"][key]
                else:
                    self.data["files"][key] = previous
                self.save()
            raise
        current, current_stamp = snapshot(self.target(key))
        if current != updated:
            entry["state"] = "conflict"
            self.save()
            raise CheckpointConflict("写入后内容与预期不一致，已保留快照，不能自动撤回。")
        entry.update(fingerprint=current_stamp, state="recorded")
        self.save()
        return result

    def inspect(self, reverse: bool = False) -> dict:
        changes, conflicts, undone = [], [], []
        for path, entry in self.data["files"].items():
            if entry["state"] == "undone":
                undone.append(path)
                continue
            if entry["before"] == entry["after"] and entry["state"] == "recorded":
                continue
            try:
                target = self.target(path)
                if target.relative_to(permissions.WORKSPACE.resolve()).as_posix() != path:
                    raise CheckpointConflict("检查点路径已被重定向。")
                if entry["state"] != "recorded" or snapshot(target) != (decode(entry["after"]), entry["fingerprint"]):
                    conflicts.append(path)
            except (OSError, ValueError):
                conflicts.append(path)
            before, after = decode(entry["before"]), decode(entry["after"])
            if reverse:
                before, after = after, before
            kind = "新增" if before is None else "删除" if after is None else "修改"
            changes.append({"path": path, "kind": kind, "diff": file_diff(path, before, after)})
        return {"finished": self.data["finished"], "changes": changes, "conflicts": conflicts,
                "undone": undone, "git_before": self.data["git_before"],
                "git_current": git_status(permissions.WORKSPACE.resolve())}

    def undo(self) -> dict:
        report = self.inspect(reverse=True)
        if not report["finished"] or report["conflicts"]:
            raise CheckpointConflict("运行尚未结束或文件存在后续变化，未撤回任何尚待处理的文件。")
        restored, removed = [], []
        for change in report["changes"]:
            path = change["path"]
            entry = self.data["files"][path]
            target = self.target(path)
            if snapshot(target) != (decode(entry["after"]), entry["fingerprint"]):
                raise CheckpointConflict("撤回期间文件已变化，已停止；此前已撤回的文件保留记录。")
            entry["state"] = "undo_pending"
            self.save()
            try:
                target = self.target(path)
                if snapshot(target) != (decode(entry["after"]), entry["fingerprint"]):
                    raise CheckpointConflict("确认后文件已变化，未覆盖。")
                before = decode(entry["before"])
                if before is None:
                    target.unlink()
                else:
                    target.write_bytes(before)
                if snapshot(self.target(path))[0] != before:
                    raise CheckpointConflict("撤回后内容与快照不一致。")
                if before is None:
                    removed.append(path)
                else:
                    restored.append(path)
            except (OSError, ValueError) as error:
                raise CheckpointConflict(f"撤回 {path} 失败（{type(error).__name__}）；已撤回 {len(restored) + len(removed)} 个文件，请用 --diff 核对中间状态。") from None
            entry["state"] = "undone"
            self.save()
        return {"ok": True, "restored": restored, "removed": removed}


def load_checkpoint(log_path: str) -> Checkpoint:
    log = Path(log_path).resolve()
    if not log.is_file():
        raise ValueError("运行日志不存在。")
    data = json.loads(log.with_suffix(".checkpoint.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or not isinstance(data.get("files"), dict):
        raise ValueError("检查点格式无效。")
    if data.get("workspace") != str(permissions.WORKSPACE.resolve()):
        raise ValueError("检查点属于其他项目，请用 --workspace 指定原项目。")
    if type(data.get("finished")) is not bool or "git_before" not in data:
        raise ValueError("检查点缺少运行状态。")
    checkpoint = Checkpoint(log, data)
    for path, entry in data["files"].items():
        target = checkpoint.target(path)
        if target.relative_to(permissions.WORKSPACE.resolve()).as_posix() != path or path == ".":
            raise ValueError("检查点路径不是规范的相对文件路径。")
        if not isinstance(entry, dict) or entry.get("state") not in ("pending", "recorded", "conflict", "undo_pending", "undone"):
            raise ValueError("检查点文件状态无效。")
        for key in ("before", "after"):
            if key not in entry or entry[key] is not None and not isinstance(entry[key], str):
                raise ValueError("检查点文件快照无效。")
            decode(entry[key])
        if entry["after"] is None or "fingerprint" not in entry:
            raise ValueError("检查点缺少文件状态信息。")
        if entry["state"] in ("recorded", "undone") and (not isinstance(entry["fingerprint"], dict) or
                set(entry["fingerprint"]) != {"mtime_ns", "mode", "size", "ino", "dev"} or
                any(type(value) is not int for value in entry["fingerprint"].values())):
            raise ValueError("检查点中的文件状态信息无效。")
    return checkpoint


_ACTIVE: Checkpoint | None = None


@contextmanager
def track_changes(log_path: Path):
    global _ACTIVE
    root = permissions.WORKSPACE.resolve()
    checkpoint = Checkpoint(log_path, {"version": 1, "workspace": str(root), "finished": False,
                                      "git_before": git_status(root), "files": {}})
    checkpoint.save()
    previous, _ACTIVE = _ACTIVE, checkpoint
    try:
        yield checkpoint
    finally:
        try:
            checkpoint.data.update(finished=True, git_after=git_status(root))
            checkpoint.save()
        finally:
            _ACTIVE = previous


def apply_change(target: Path, updated: bytes, operation):
    if _ACTIVE is None:
        return operation()
    return _ACTIVE.change(target, updated, operation)
