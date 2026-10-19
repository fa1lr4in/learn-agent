"""Read SSE fragments and assemble one complete chat response."""

import json
from collections.abc import Callable, Iterable


def iter_sse_data(lines: Iterable[bytes]) -> Iterable[str]:
    data = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield "\n".join(data)


def read_stream(lines: Iterable[bytes], on_delta: Callable[[dict], None] | None = None) -> dict:
    """Return only after a finish reason and [DONE]; fragments never run tools."""
    response = {"object": "chat.completion"}
    content = []
    calls = {}
    finish_reason = None
    done = False
    for data in iter_sse_data(lines):
        if data == "[DONE]":
            done = True
            break
        chunk = json.loads(data)
        if not isinstance(chunk, dict) or chunk.get("error"):
            raise ValueError("模型流式返回格式无效或包含错误；未执行本轮工具。")
        for key in ("id", "model", "created", "system_fingerprint"):
            if key in chunk:
                response.setdefault(key, chunk[key])
        if chunk.get("usage") is not None:
            response["usage"] = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("流式回复只支持一个候选回答。")
        if not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict) or type(choice.get("index", 0)) is not int or choice.get("index", 0) != 0:
            raise ValueError("流式候选回答的编号无效。")
        delta = choice.get("delta")
        if not isinstance(delta, dict) or delta.get("role") not in (None, "assistant"):
            raise ValueError("模型流式消息片段无效。")
        text = delta.get("content")
        fragments = delta.get("tool_calls")
        if finish_reason is not None and (text or fragments):
            raise ValueError("结束信号后仍有新增内容，未执行本轮工具。")
        if text is not None:
            if not isinstance(text, str):
                raise ValueError("模型流式文本不是字符串。")
            content.append(text)
        if fragments is not None:
            if not isinstance(fragments, list):
                raise ValueError("流式工具片段必须是列表。")
            for fragment in fragments:
                if not isinstance(fragment, dict) or type(fragment.get("index")) is not int or fragment["index"] < 0:
                    raise ValueError("流式工具片段缺少有效的 index。")
                call = calls.setdefault(fragment["index"], {"id": "", "type": "", "function": {"name": "", "arguments": ""}})
                for key in ("id", "type"):
                    value = fragment.get(key)
                    if value is not None:
                        if not isinstance(value, str) or (call[key] and value and call[key] != value):
                            raise ValueError("同一流式工具的身份信息不一致。")
                        if value:
                            call[key] = value
                function = fragment.get("function") or {}
                if not isinstance(function, dict):
                    raise ValueError("流式工具函数片段无效。")
                for key in ("name", "arguments"):
                    value = function.get(key)
                    if value is not None:
                        if not isinstance(value, str):
                            raise ValueError("流式工具名称和参数必须是字符串片段。")
                        call["function"][key] += value
        if delta and on_delta is not None:
            on_delta(delta)
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str) or not reason or (finish_reason is not None and finish_reason != reason):
                raise ValueError("流式回复的结束原因无效或不一致。")
            finish_reason = reason
    if not done or finish_reason is None:
        raise ValueError("流式回复未完整结束；缺少结束原因或 [DONE]，未执行本轮工具。")
    ordered = [calls[index] for index in sorted(calls)]
    if finish_reason == "tool_calls":
        if not ordered or any(not call["id"].strip() or call["type"] != "function" or not call["function"]["name"].strip() for call in ordered):
            raise ValueError("流式工具请求不完整，未执行本轮工具。")
    elif finish_reason == "stop" and ordered:
        raise ValueError("正常结束的流式回复仍包含工具请求，未执行本轮工具。")
    message = {"role": "assistant", "content": "".join(content) if content else None}
    if ordered:
        message["tool_calls"] = ordered
    response["choices"] = [{"index": 0, "message": message, "finish_reason": finish_reason}]
    return response
