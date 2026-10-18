"""JSONL event recording and message-history restoration."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from . import permissions


def record_event(log_file: TextIO | None, event: str, **details) -> None:
    """Append and flush one JSON event without receiving API credentials."""
    if log_file is None:
        return
    entry = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **details}
    log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log_file.flush()


def load_history(path: str) -> list[dict]:
    """Restore completed or request-limited history without replaying tools."""
    try:
        entries = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
        if not isinstance(entries[0], dict) or entries[0].get("event") != "run_start":
            raise ValueError("日志缺少运行开始记录，无法确认工作目录。")
        saved_root = entries[0].get("workspace", str(permissions.DEFAULT_WORKSPACE.resolve()))
        if not isinstance(saved_root, str) or not Path(saved_root).is_absolute():
            raise ValueError("日志中的工作目录必须是绝对路径。")
        if Path(saved_root).resolve() != permissions.WORKSPACE.resolve():
            raise ValueError("日志属于另一个工作目录；请用 --workspace 指定原项目目录后恢复。无目录记录的旧日志只能在默认练习目录恢复。")
        if entries[-1]["event"] != "run_end" or entries[-1]["reason"] not in ("stop", "max_model_calls"):
            raise ValueError("目前只支持恢复正常结束或因模型请求次数上限停止的会话。")
        end_reason = entries[-1]["reason"]
        request = next(entry for entry in reversed(entries) if entry["event"] == "model_request")
        response = next(entry for entry in reversed(entries) if entry["event"] == "model_response")
        choice = response["response"]["choices"][0]
        expected_finish = "stop" if end_reason == "stop" else "tool_calls"
        if request["step"] != response["step"] or choice["finish_reason"] != expected_finish:
            raise ValueError("日志中的最后一次请求、回复与结束原因不对应。")
        messages = request["payload"]["messages"]
        if not isinstance(messages, list) or not messages or messages[0]["role"] != "system":
            raise ValueError("日志缺少完整的消息历史。")
        final = choice["message"]
        if not isinstance(final, dict):
            raise ValueError("日志中的最终回答格式无效。")
        if end_reason == "stop":
            if final.get("tool_calls"):
                raise ValueError("日志的最终回答仍包含未完成的工具请求。")
            history = [*messages[1:], {"role": "assistant", "content": final.get("content") or ""}]
        else:
            calls = final.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                raise ValueError("达到请求次数上限的回复缺少工具请求。")
            if any(entry["event"] == "tool_result" and entry["step"] == request["step"] for entry in entries):
                raise ValueError("停止轮次已有工具执行结果，无法按未执行状态恢复。")
            history = [*messages[1:], {"role": "assistant", "content": final.get("content"), "tool_calls": calls}]
            for call in calls:
                status = {
                    "ok": False, "tool": call["function"]["name"], "source": "resume",
                    "error": {"type": "NotExecuted", "message": "上次运行达到模型请求次数上限，该请求未执行；如仍需要，请重新提出工具调用。"},
                }
                history.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(status, ensure_ascii=False)})
        pending = set()
        for message in history:
            if not isinstance(message, dict):
                raise ValueError("日志中的消息格式无效。")
            role = message["role"]
            if role == "tool":
                call_id = message["tool_call_id"]
                if call_id not in pending:
                    raise ValueError("日志中的工具结果没有对应的请求。")
                pending.remove(call_id)
            else:
                if pending or role not in ("user", "assistant"):
                    raise ValueError("日志中的消息历史或工具结果不完整。")
                if role == "assistant":
                    for call in message.get("tool_calls") or []:
                        call_id = call["id"]
                        if not isinstance(call_id, str) or not call_id.strip() or call_id in pending:
                            raise ValueError("日志中的工具调用 ID 无效。")
                        pending.add(call_id)
        if pending:
            raise ValueError("日志仍有未完成的工具请求。")
        return history
    except (KeyError, IndexError, TypeError, StopIteration, json.JSONDecodeError):
        raise ValueError("日志格式不完整或无效，无法恢复会话。") from None
