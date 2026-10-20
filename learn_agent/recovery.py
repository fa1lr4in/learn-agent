"""Inspect stopped runs and rebuild history without replaying old operations."""

import json
from dataclasses import dataclass
from pathlib import Path

from .events import validate_history
from .sessions import project_run
from .tasks import replay_events


RECOVERABLE_REASONS = ("cancelled", "user_denied", "policy_denied", "error", "abnormal_finish", "max_total_tokens", "usage_unavailable", "context_limit", "plan_incomplete")
RECOVERY_LABELS = {"recorded": "已有结果", "not_executed": "确定未执行", "unknown": "结果未知，需核对"}


@dataclass
class RecoveryPlan:
    history: list[dict]
    tools: list[dict]
    reason: str
    note: str = ""

    def require_safe(self) -> None:
        if self.reason == "unfinished":
            raise ValueError("日志没有 run_end，无法确认原任务已经停止；请先核对进程和现场，不直接恢复。")
        unknown = [tool["name"] for tool in self.tools if tool["status"] == "unknown"]
        if unknown:
            raise ValueError("以下工具结果未知或进程清理未确认：" + ", ".join(unknown) + "。请先核对现场，再作为新任务决定如何处理；未请求模型或重放工具。")


def load_recovery(path: str, workspace: Path) -> RecoveryPlan:
    """Prepare a reviewed transcript; a caller must require_safe before use."""
    try:
        source = Path(path).resolve()
        entries = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
        status, _ = replay_events(entries)
        saved_root = entries[0].get("workspace")
        if not isinstance(saved_root, str) or not Path(saved_root).is_absolute() or Path(saved_root).resolve() != workspace.resolve():
            raise ValueError("恢复日志的工作目录不匹配；请用 --workspace 指定原项目目录。")
        reason = status.reason if status.ended else "unfinished"
        if reason not in (*RECOVERABLE_REASONS, "unfinished"):
            raise ValueError("正常结束或请求次数超限的任务请使用 --resume；--recover 用于取消、异常和 token 预算暂停的任务。")
        request = next((entry for entry in reversed(entries) if entry["event"] == "model_request"), None)
        messages = request.get("full_messages", request["payload"]["messages"]) if request else entries[0].get("initial_messages")
        if not messages or messages[0]["role"] != "system":
            raise ValueError("日志缺少完整请求，不能恢复。")
        history = list(messages[1:])
        validate_history(history)
        _, records = project_run([{**entries[0], "log_path": str(source)}, *entries[1:]])
        started = {(entry["step"], entry["tool_call_id"]) for entry in entries if entry["event"] == "tool_started"}
        if started - {(tool.step, tool.call_id) for tool in records}:
            raise ValueError("工具开始记录缺少对应的模型请求，不能恢复。")
        reliable_starts = entries[0].get("recovery_version") == 1
        report = []
        results = {}
        for tool in records:
            key = (tool.step, tool.call_id)
            if tool.result is not None:
                state = "unknown" if tool.result.get("cleanup_succeeded") is False else "recorded"
            elif key in started or (tool.approved is True and not reliable_starts):
                state = "unknown"
            else:
                state = "not_executed"
            report.append({"step": tool.step, "call_id": tool.call_id, "name": tool.name, "status": state})
            results[key] = (tool, state)

        response = next((entry for entry in reversed(entries)
                         if entry["event"] == "model_response" and request is not None and entry["step"] == request["step"]), None)
        note = ""
        if response is None:
            note = ("尚未请求任务模型；摘要不会作为业务回复恢复，可以显式继续。" if request is None else
                    "最后一次模型请求没有完整回复；可以显式重新请求模型，不自动重试旧工具。")
        else:
            choice = response["response"]["choices"][0]
            message = choice["message"]
            if choice["finish_reason"] == "tool_calls":
                calls = message.get("tool_calls")
                if not isinstance(calls, list) or not calls:
                    raise ValueError("模型工具请求不完整，不能直接恢复。")
                history.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
                for call in calls:
                    function = call["function"]
                    if not isinstance(json.loads(function["arguments"]), dict):
                        raise ValueError("模型工具参数无效，不能直接恢复。")
                    tool, state = results[(request["step"], call["id"])]
                    result = tool.result
                    if result is None:
                        result = {"ok": False, "tool": function["name"], "source": "recovery", "error": {
                            "type": "NotExecuted" if state == "not_executed" else "ExecutionUnknown",
                            "message": "上次该请求未执行；需要时重新提出并授权。" if state == "not_executed" else "已进入执行阶段但没有结果，可能已有副作用，必须先核对现场。",
                        }}
                    history.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
            elif choice["finish_reason"] == "stop":
                if message.get("tool_calls"):
                    raise ValueError("最终回答仍包含工具请求，不能直接恢复。")
                history.append({"role": "assistant", "content": message.get("content") or ""})
            else:
                note = "最后一条模型回复异常或被截断，未作为完整回答或可执行工具请求恢复。"
        validate_history(history)
        return RecoveryPlan(history, report, reason, note)
    except (KeyError, IndexError, TypeError, StopIteration, json.JSONDecodeError):
        raise ValueError("恢复日志或工具参数不完整、无效；未请求模型或执行工具。") from None
