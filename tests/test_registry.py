import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import tool_call
from learn_agent import loop, tools


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        registry_patch = patch.dict(tools.TOOL_REGISTRY)
        registry_patch.start()
        self.addCleanup(registry_patch.stop)
        self.payload = {"messages": [{"role": "user", "content": "测试注册工具"}]}
        self.final = {"choices": [{"finish_reason": "stop", "message": {"content": "完成。"}}]}
        self.execute = Mock(side_effect=lambda text: {"content": text})

    def echo_tool(self, read_only=False):
        def validate(arguments):
            if not isinstance(arguments["text"], str):
                raise ValueError("text 必须是字符串。")

        return tools.Tool(
            schema={"type": "function", "function": {
                "name": "echo", "description": "返回输入文本，仅用于离线测试。",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                               "required": ["text"], "additionalProperties": False},
            }},
            validate=validate,
            prepare=lambda arguments: tools.PreparedTool("返回输入文本", lambda: self.execute(arguments["text"])),
            read_only=read_only,
        )

    def response(self, calls):
        return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for call_id, name, arguments in calls
        ]}}]}

    def test_registered_tool_reaches_preview_loop_permissions_history_and_log(self):
        tools.register_tool(self.echo_tool(read_only=True))
        reply = self.response([("echo-1", "echo", {"text": "你好"})])
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            log_dir = Path(directory) / "logs"
            with patch.object(tool_call, "LOG_DIR", log_dir), patch("builtins.input") as approval:
                with patch.object(sys, "argv", ["tool_call.py", "--preview", "返回文本"]):
                    with patch("tool_call.get_api_key") as key, patch("learn_agent.providers.call_model") as model:
                        with contextlib.redirect_stdout(io.StringIO()) as output:
                            tool_call.main()
                preview = json.loads(output.getvalue().split("\n", 1)[1].split("\n\n预览结束", 1)[0])
                self.assertEqual(preview["tools"], tools.tool_schemas())
                self.assertIn("echo", [item["function"]["name"] for item in preview["tools"]])
                key.assert_not_called()
                model.assert_not_called()
                self.execute.assert_not_called()
                self.assertFalse(log_dir.exists())

                with patch.object(sys, "argv", ["tool_call.py", "--auto-read", "返回文本"]):
                    with patch("tool_call.get_api_key", return_value="offline-placeholder"):
                        with patch("learn_agent.providers.call_model", side_effect=[reply, self.final]) as model:
                            with contextlib.redirect_stdout(io.StringIO()):
                                tool_call.main()
                events = [json.loads(line) for line in next(log_dir.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        approval.assert_not_called()
        self.execute.assert_called_once_with("你好")
        self.assertEqual(model.call_args_list[0].args[0]["tools"], preview["tools"])
        history = model.call_args_list[1].args[0]["messages"]
        self.assertEqual(history[-1]["tool_call_id"], "echo-1")
        self.assertEqual(json.loads(history[-1]["content"]), {"ok": True, "tool": "echo", "content": "你好"})
        decision = next(event for event in events if event["event"] == "tool_approval")
        self.assertEqual((decision["tool"], decision["approved"], decision["source"]), ("echo", True, "auto_read"))
        self.assertEqual((events[-1]["event"], events[-1]["reason"]), ("run_end", "stop"))

    def test_duplicate_registration_does_not_replace_existing_tool(self):
        original = tools.TOOL_REGISTRY["read_file"]
        with self.assertRaises(ValueError):
            tools.register_tool(original)
        self.assertIs(tools.TOOL_REGISTRY["read_file"], original)

    def test_new_tool_requires_confirmation_by_default(self):
        tools.register_tool(self.echo_tool())
        reply = self.response([("echo-1", "echo", {"text": "你好"})])
        with patch("learn_agent.providers.call_model", return_value=reply) as model:
            with patch("builtins.input", return_value="n") as approval, contextlib.redirect_stdout(io.StringIO()):
                reason = loop.run_agent(self.payload, "offline-placeholder", auto_read=True)
        self.assertEqual(reason, "user_denied")
        model.assert_called_once()
        approval.assert_called_once()
        self.execute.assert_not_called()

    def test_registered_tool_validation_blocks_entire_batch_before_execution(self):
        tools.register_tool(self.echo_tool(read_only=True))
        for arguments in ({}, {"text": "你好", "extra": True}, {"text": 123}):
            with self.subTest(arguments=arguments):
                reply = self.response([("read-1", "read_file", {"path": "project.txt"}), ("echo-1", "echo", arguments)])
                with patch("learn_agent.providers.call_model", return_value=reply), patch("learn_agent.tools.read_file") as reader:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                        loop.run_agent(self.payload, "offline-placeholder", auto_read=True)
                reader.assert_not_called()
                approval.assert_not_called()
                self.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
