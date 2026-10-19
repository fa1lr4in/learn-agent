"""Task status derived from model, approval, tool and lifecycle events."""

from dataclasses import dataclass


STATE_LABELS = {
    "running": "运行中",
    "waiting_approval": "等待确认",
    "completed": "完成",
    "failed": "失败",
    "cancelled": "取消",
    "paused": "暂停",
}
END_STATES = {
    "stop": "completed",
    "error": "failed",
    "abnormal_finish": "failed",
    "user_denied": "cancelled",
    "cancelled": "cancelled",
    "max_model_calls": "paused",
    "max_total_tokens": "paused",
    "usage_unavailable": "paused",
}


@dataclass
class TaskStatus:
    state: str = "running"
    reason: str | None = None
    ended: bool = False

    def apply(self, entry: dict) -> None:
        """Apply one observed event; never execute a model request or tool."""
        event = entry["event"]
        if self.ended:
            raise ValueError("任务已经结束，不能继续记录或执行后续操作。")
        if event == "run_end":
            reason = entry["reason"]
            if reason not in END_STATES:
                raise ValueError("无法识别任务结束原因。")
            state = END_STATES[reason]
            if self.state == "cancelled" and state != "cancelled":
                raise ValueError("已拒绝工具，任务不能再标记为完成或继续执行。")
            if self.state == "waiting_approval" and state in ("completed", "paused"):
                raise ValueError("仍在等待确认，不能标记为完成或次数超限。")
            self.state, self.reason, self.ended = state, reason, True
        elif event == "tool_approval":
            if self.state not in ("running", "waiting_approval") or not isinstance(entry["approved"], bool):
                raise ValueError("工具确认事件与当前任务状态不一致。")
            self.state = "running" if entry["approved"] else "cancelled"
        else:
            if self.state != "running":
                raise ValueError("任务未处于运行状态，不能开始模型请求或工具操作。")
            if event == "tool_waiting":
                self.state = "waiting_approval"
            elif event not in ("run_start", "model_request", "model_delta", "model_response", "tool_started", "tool_result"):
                raise ValueError(f"无法识别任务事件：{event}")


def replay_events(entries: list[dict]) -> tuple[TaskStatus, list[tuple[str, str]]]:
    """Rebuild status and transitions from a complete log or its saved prefix."""
    if not entries or not isinstance(entries[0], dict) or entries[0].get("event") != "run_start":
        raise ValueError("日志缺少任务开始记录。")
    status = TaskStatus()
    transitions = []
    previous = None
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or "event" not in entry:
            raise ValueError("任务事件格式无效。")
        if index and entry["event"] == "run_start":
            raise ValueError("同一份任务日志不能重复开始。")
        status.apply(entry)
        if "task_state" in entry and entry["task_state"] != status.state:
            raise ValueError("日志记录的状态与事件推导结果不一致。")
        if status.state != previous:
            transitions.append((entry["event"], status.state))
            previous = status.state
    return status, transitions
