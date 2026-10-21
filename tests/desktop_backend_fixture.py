"""Real HTTP/process lifecycle fixture with deterministic model replies; never calls a model API."""

import json
from pathlib import Path

import api as launcher
from learn_agent.providers import DeepSeekProvider, ModelResponse


class OfflineProvider(DeepSeekProvider):
    name = "offline-lifecycle"

    def complete(self, payload, api_key, on_delta=None):
        if payload["messages"][-1]["role"] == "user":
            command = ["python", "main.py"] if "[hang]" in payload["messages"][-1]["content"] else [
                "python", "-m", "unittest", "-v", "test_stats.py"]
            tools = [("run_command", {"command": command, "timeout": 120}), ("run_python", {"path": "main.py"})]
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call-{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
                for index, (name, arguments) in enumerate(tools)]}
            reason = "tool_calls"
        else:
            results = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
            content = "本地固定回复验证（未调用 DeepSeek）：\n" + json.dumps(results, ensure_ascii=False)
            if on_delta:
                on_delta({"content": content})
            message, reason = {"role": "assistant", "content": content}, "stop"
        return ModelResponse(message, reason, {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})


if __name__ == "__main__":
    original = launcher.create_app

    def create_offline_app(*args, **kwargs):
        return original(*args, **kwargs, provider=OfflineProvider())

    launcher.create_app = create_offline_app
    launcher.LOG_DIR = Path.cwd() / "logs"
    launcher.main()
