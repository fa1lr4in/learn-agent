"""Provider boundary: build requests and return a common model response."""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .model import call_model


@dataclass(frozen=True)
class ModelResponse:
    message: dict
    finish_reason: str
    usage: dict | None = None
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """Keep the existing log format so old history readers still work."""
        result = {**self.metadata, "choices": [{"message": self.message, "finish_reason": self.finish_reason}]}
        if self.usage is not None:
            result["usage"] = self.usage
        return result


class ModelProvider(Protocol):
    name: str

    def build_request(self, messages: list[dict], *, tools: list[dict] | None = None,
                      stream: bool = False, max_tokens: int = 2048, json_mode: bool = False,
                      model: str | None = None) -> dict: ...

    def complete(self, payload: dict, api_key: str,
                 on_delta: Callable[[dict], None] | None = None) -> ModelResponse: ...


class DeepSeekProvider:
    name = "deepseek"

    def build_request(self, messages: list[dict], *, tools: list[dict] | None = None,
                      stream: bool = False, max_tokens: int = 2048, json_mode: bool = False,
                      model: str | None = None) -> dict:
        payload = {
            "model": model if model is not None else os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
            "messages": messages, "stream": stream, "thinking": {"type": "disabled"}, "max_tokens": max_tokens,
        }
        if tools is not None:
            payload.update(tools=tools, tool_choice="auto")
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def complete(self, payload: dict, api_key: str,
                 on_delta: Callable[[dict], None] | None = None) -> ModelResponse:
        # The existing HTTP/SSE reader closes the connection before returning.
        response = call_model(payload, api_key, on_delta=on_delta) if on_delta is not None else call_model(payload, api_key)
        return self.parse_response(response)

    @staticmethod
    def parse_response(response: dict) -> ModelResponse:
        try:
            choices = response["choices"]
            if response.get("error") or not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            message = choices[0]["message"]
            finish_reason = choices[0]["finish_reason"]
            if not isinstance(message, dict) or not isinstance(finish_reason, str) or not finish_reason:
                raise ValueError
            if message.get("role") not in (None, "assistant") or (message.get("content") is not None and not isinstance(message["content"], str)):
                raise ValueError
            if message.get("tool_calls") is not None and not isinstance(message["tool_calls"], list):
                raise ValueError
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            raise ValueError("DeepSeek 回复格式无效或缺少完整结束原因；未执行本轮工具。") from None
        usage = response.get("usage")
        return ModelResponse(message, finish_reason, usage if isinstance(usage, dict) else None,
                             {key: value for key, value in response.items() if key not in ("choices", "usage")})
