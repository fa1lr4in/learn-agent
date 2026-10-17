import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call


class RunPythonTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="learn-agent-test-")
        self.workspace = Path(self.directory.name)
        self.assertEqual(self.workspace.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.directory.cleanup)
        workspace_patch = patch.object(tool_call, "WORKSPACE", self.workspace)
        workspace_patch.start()
        self.addCleanup(workspace_patch.stop)
        self.payload = {"messages": [{"role": "system", "content": "test"}, {"role": "user", "content": "run"}]}
        self.response = {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{
                "id": "call_run", "type": "function", "function": {
                    "name": "run_python", "arguments": json.dumps({"path": "main.py"}),
                },
            }],
        }}]}
        self.final = {"choices": [{"message": {"content": "已根据执行结果回答。"}, "finish_reason": "stop"}]}

    def test_agent_writes_and_runs_real_python_before_answering(self):
        code = 'print("计算结果：", sum(range(1, 101)))\n'
        writing = copy.deepcopy(self.response)
        writing["choices"][0]["message"]["tool_calls"][0] = {
            "id": "call_write", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "main.py", "content": code}),
            },
        }
        log_file = io.StringIO()
        with patch("tool_call.call_model", side_effect=[writing, self.response, self.final]) as model:
            with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                reason = tool_call.run_agent(self.payload, "offline-placeholder", log_file, auto_read=True)
        self.assertEqual(reason, "stop")
        self.assertEqual(model.call_count, 3)
        self.assertEqual(approval.call_count, 2)
        self.assertIn("创建 main.py", approval.call_args_list[0].args[0])
        self.assertIn("运行 main.py", approval.call_args_list[1].args[0])
        self.assertEqual((self.workspace / "main.py").read_text(encoding="utf-8"), code)
        history = model.call_args_list[-1].args[0]["messages"]
        self.assertEqual([history[index]["tool_call_id"] for index in (3, 5)], ["call_write", "call_run"])
        result = json.loads(history[-1]["content"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "计算结果： 5050")
        self.assertEqual(result["stderr"], "")
        entries = [json.loads(line) for line in log_file.getvalue().splitlines()]
        decisions = [entry for entry in entries if entry["event"] == "tool_approval"]
        self.assertEqual([entry["source"] for entry in decisions], ["user", "user"])
        self.assertEqual(len(self.payload["messages"]), 2)

    def test_child_uses_workspace_and_does_not_inherit_deepseek_key(self):
        (self.workspace / "input.txt").write_text("练习资料", encoding="utf-8")
        tool_call.write_file("main.py", 'import os\nfrom pathlib import Path\nprint(Path("input.txt").read_text(encoding="utf-8"))\nprint("DEEPSEEK_API_KEY" in os.environ)\n')
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline-sensitive-test-key"}):
            completed = tool_call.run_python("main.py")
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "offline-sensitive-test-key")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.splitlines(), ["练习资料", "False"])
        self.assertEqual(completed.stderr, "")

    def test_same_reply_can_write_then_run_with_separate_results(self):
        self.response["choices"][0]["message"]["tool_calls"].insert(0, {
            "id": "call_write", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "main.py", "content": "print(sum(range(1, 101)))\n"}),
            },
        })
        with patch("tool_call.call_model", side_effect=[self.response, self.final]) as model:
            with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                reason = tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
        self.assertEqual(reason, "stop")
        self.assertEqual(model.call_count, 2)
        self.assertEqual(approval.call_count, 2)
        results = model.call_args_list[-1].args[0]["messages"][3:]
        self.assertEqual([entry["tool_call_id"] for entry in results], ["call_write", "call_run"])
        self.assertTrue(all(json.loads(entry["content"])["ok"] for entry in results))
        self.assertEqual(json.loads(results[-1]["content"])["stdout"].strip(), "5050")

    def test_runtime_and_syntax_errors_are_returned_to_model(self):
        for code, error_type in [('print("开始")\nprint(1 / 0)\n', "ZeroDivisionError"), ("def broken(:\n    pass\n", "SyntaxError")]:
            with self.subTest(error_type=error_type):
                tool_call.write_file("main.py", code)
                with patch("tool_call.call_model", side_effect=[self.response, self.final]) as model:
                    with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                        tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
                approval.assert_called_once()
                result = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
                self.assertFalse(result["ok"])
                self.assertNotEqual(result["exit_code"], 0)
                self.assertIn(error_type, result["stderr"])
                if error_type == "ZeroDivisionError":
                    self.assertIn("开始", result["stdout"])

    def test_timeout_terminates_child_and_returns_tool_error(self):
        tool_call.write_file("main.py", "import time\ntime.sleep(30)\n")
        with patch.object(tool_call, "RUN_TIMEOUT", 0.3), patch("tool_call.call_model", side_effect=[self.response, self.final]) as model:
            with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
                reason = tool_call.run_agent(self.payload, "offline-placeholder")
        self.assertEqual(reason, "stop")
        self.assertEqual(model.call_count, 2)
        result = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "TimeoutExpired")
        self.assertNotIn("exit_code", result)

    def test_missing_script_does_not_start_process(self):
        with patch("tool_call.call_model", side_effect=[self.response, self.final]) as model:
            with patch("tool_call.subprocess.run") as process, patch("builtins.input", return_value="y"):
                with contextlib.redirect_stdout(io.StringIO()):
                    tool_call.run_agent(self.payload, "offline-placeholder")
        process.assert_not_called()
        result = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "FileNotFoundError")
        self.assertEqual(result["path"], "main.py")

    def test_invalid_run_parameters_are_rejected_before_confirmation(self):
        for arguments in [{}, {"path": "../chat.py"}, {"path": "report.md"}, {"path": "main.py", "args": ["-c", "print(1)"]}]:
            with self.subTest(arguments=arguments):
                self.response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(arguments)
                with patch("tool_call.call_model", return_value=self.response), patch("tool_call.subprocess.run") as process:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                        tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
                approval.assert_not_called()
                process.assert_not_called()
        with patch("tool_call.subprocess.run") as process, self.assertRaises(ValueError):
            tool_call.run_python("../chat.py")
        process.assert_not_called()

    def test_runner_rejects_symbolic_link(self):
        with patch.object(Path, "is_symlink", return_value=True):
            with patch("tool_call.subprocess.run") as process, self.assertRaises(ValueError):
                tool_call.run_python("main.py")
        process.assert_not_called()

    def test_auto_read_does_not_bypass_execution_confirmation(self):
        log_file = io.StringIO()
        with patch("tool_call.call_model", return_value=self.response) as model, patch("tool_call.run_python") as runner:
            with patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
                reason = tool_call.run_agent(self.payload, "offline-placeholder", log_file, auto_read=True)
        self.assertEqual(reason, "user_denied")
        model.assert_called_once()
        runner.assert_not_called()
        decision = json.loads(log_file.getvalue().splitlines()[-1])
        self.assertEqual(decision["source"], "user")
        self.assertFalse(decision["approved"])

    def test_preview_shows_runner_without_executing_or_creating_logs(self):
        log_dir = self.workspace / "logs"
        with patch.object(tool_call, "LOG_DIR", log_dir), patch.object(sys, "argv", ["tool_call.py", "--preview"]):
            with patch("tool_call.get_api_key") as key, patch("tool_call.call_model") as model, patch("tool_call.run_python") as runner:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    tool_call.main()
        payload, _ = json.JSONDecoder().raw_decode(output.getvalue().split("\n", 1)[1])
        self.assertIn("run_python", [item["function"]["name"] for item in payload["tools"]])
        self.assertFalse(log_dir.exists())
        self.assertFalse((self.workspace / "main.py").exists())
        key.assert_not_called()
        model.assert_not_called()
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
