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

    def test_agent_repairs_script_after_real_error_and_runs_again(self):
        broken_code = "numbers = list(range(1, 101))\nprint(sum(nubmers))\n"
        fixed_code = "numbers = list(range(1, 101))\nprint(sum(numbers))\n"
        responses = []
        for call_id, name, arguments in [
            ("call_write_broken", "write_file", {"path": "main.py", "content": broken_code}),
            ("call_run_broken", "run_python", {"path": "main.py"}),
            ("call_write_fixed", "write_file", {"path": "main.py", "content": fixed_code}),
            ("call_run_fixed", "run_python", {"path": "main.py"}),
        ]:
            response = copy.deepcopy(self.response)
            response["choices"][0]["message"]["tool_calls"][0] = {
                "id": call_id, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments),
                },
            }
            responses.append(response)
        log_file = io.StringIO()
        with patch("tool_call.call_model", side_effect=[*responses, self.final]) as model:
            with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                reason = tool_call.run_agent(self.payload, "offline-placeholder", log_file, auto_read=True)
        self.assertEqual(reason, "stop")
        self.assertEqual(model.call_count, 5)
        self.assertEqual(approval.call_count, 4)
        self.assertIn("覆盖现有 main.py", approval.call_args_list[2].args[0])
        failure = json.loads(model.call_args_list[2].args[0]["messages"][-1]["content"])
        self.assertFalse(failure["ok"])
        self.assertNotEqual(failure["exit_code"], 0)
        self.assertIn("NameError", failure["stderr"])
        self.assertIn("nubmers", failure["stderr"])
        self.assertEqual(failure["stdout"], "")
        history = model.call_args_list[-1].args[0]["messages"]
        results = [message for message in history if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in results], [
            "call_write_broken", "call_run_broken", "call_write_fixed", "call_run_fixed",
        ])
        self.assertEqual(json.loads(results[1]["content"]), failure)
        success = json.loads(results[-1]["content"])
        self.assertTrue(success["ok"])
        self.assertEqual(success["exit_code"], 0)
        self.assertEqual(success["stdout"].strip(), "5050")
        self.assertEqual(success["stderr"], "")
        self.assertEqual((self.workspace / "main.py").read_text(encoding="utf-8"), fixed_code)
        entries = [json.loads(line) for line in log_file.getvalue().splitlines()]
        decisions = [entry for entry in entries if entry["event"] == "tool_approval"]
        self.assertEqual([entry["source"] for entry in decisions], ["user"] * 4)
        recorded_results = [entry["result"] for entry in entries if entry["event"] == "tool_result"]
        self.assertEqual(recorded_results, [json.loads(message["content"]) for message in results])

    def test_agent_repairs_logic_error_using_fixed_acceptance_tests(self):
        fixture = Path(tool_call.__file__).parent / "workspace" / "test_main.py"
        test_content = fixture.read_bytes()
        (self.workspace / "test_main.py").write_bytes(test_content)
        broken_code = "def sum_to(n):\n    return sum(range(1, n))\n\nif __name__ == '__main__':\n    print(sum_to(100))\n"
        fixed_code = broken_code.replace("range(1, n)", "range(1, n + 1)")
        tool_call.write_file("main.py", broken_code)
        ordinary_run = tool_call.run_python("main.py")
        self.assertEqual(ordinary_run.returncode, 0)
        self.assertEqual(ordinary_run.stdout.strip(), "4950")
        self.assertEqual(ordinary_run.stderr, "")
        responses = []
        for call_id, name, arguments in [
            ("call_write_broken", "write_file", {"path": "main.py", "content": broken_code}),
            ("call_test_broken", "run_python", {"path": "test_main.py"}),
            ("call_write_fixed", "write_file", {"path": "main.py", "content": fixed_code}),
            ("call_test_fixed", "run_python", {"path": "test_main.py"}),
            ("call_run_fixed", "run_python", {"path": "main.py"}),
        ]:
            response = copy.deepcopy(self.response)
            response["choices"][0]["message"]["tool_calls"][0] = {
                "id": call_id, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments),
                },
            }
            responses.append(response)
        log_file = io.StringIO()
        with patch("tool_call.call_model", side_effect=[*responses, self.final]) as model:
            with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                reason = tool_call.run_agent(self.payload, "offline-placeholder", log_file, auto_read=True)
        self.assertEqual(reason, "stop")
        self.assertEqual(model.call_count, 6)
        self.assertEqual(approval.call_count, 5)
        self.assertIn("运行 test_main.py", approval.call_args_list[1].args[0])
        failure = json.loads(model.call_args_list[2].args[0]["messages"][-1]["content"])
        self.assertEqual(failure["path"], "test_main.py")
        self.assertFalse(failure["ok"])
        self.assertNotEqual(failure["exit_code"], 0)
        self.assertIn("4950 != 5050", failure["stderr"])
        self.assertIn("FAILED", failure["stderr"])
        success = json.loads(model.call_args_list[4].args[0]["messages"][-1]["content"])
        self.assertTrue(success["ok"])
        self.assertEqual(success["exit_code"], 0)
        self.assertIn("Ran 4 tests", success["stderr"])
        self.assertIn("OK", success["stderr"])
        final_run = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
        self.assertTrue(final_run["ok"])
        self.assertEqual(final_run["path"], "main.py")
        self.assertEqual(final_run["exit_code"], 0)
        self.assertEqual(final_run["stdout"].strip(), "5050")
        self.assertEqual(final_run["stderr"], "")
        self.assertEqual((self.workspace / "main.py").read_text(encoding="utf-8"), fixed_code)
        self.assertEqual((self.workspace / "test_main.py").read_bytes(), test_content)
        entries = [json.loads(line) for line in log_file.getvalue().splitlines()]
        decisions = [entry for entry in entries if entry["event"] == "tool_approval"]
        self.assertEqual([entry["source"] for entry in decisions], ["user"] * 5)
        executions = [entry["result"] for entry in entries if entry["event"] == "tool_result" and entry["tool"] == "run_python"]
        self.assertEqual(executions, [failure, success, final_run])

    def test_existing_scripts_are_listed_read_and_verified_in_a_fresh_run(self):
        code = "def sum_to(n):\n    return sum(range(1, n + 1))\n\nif __name__ == '__main__':\n    print(sum_to(100))\n"
        (self.workspace / "main.py").write_text(code, encoding="utf-8")
        fixture = Path(tool_call.__file__).parent / "workspace" / "test_main.py"
        test_content = fixture.read_text(encoding="utf-8")
        (self.workspace / "test_main.py").write_text(test_content, encoding="utf-8")
        responses = []
        for calls in [
            [("call_list", "list_files", {})],
            [("call_read_main", "read_file", {"path": "main.py"}), ("call_read_tests", "read_file", {"path": "test_main.py"})],
            [("call_run_tests", "run_python", {"path": "test_main.py"}), ("call_run_main", "run_python", {"path": "main.py"})],
        ]:
            response = copy.deepcopy(self.response)
            response["choices"][0]["message"]["tool_calls"] = [
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
                for call_id, name, arguments in calls
            ]
            responses.append(response)
        log_file = io.StringIO()
        with patch("tool_call.call_model", side_effect=[*responses, self.final]) as model:
            with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                reason = tool_call.run_agent(self.payload, "offline-placeholder", log_file, auto_read=True)
        self.assertEqual(reason, "stop")
        self.assertEqual(model.call_count, 4)
        self.assertEqual(approval.call_count, 2)
        listed = json.loads(model.call_args_list[1].args[0]["messages"][-1]["content"])
        self.assertEqual(listed["files"], ["main.py", "test_main.py"])
        self.assertEqual(listed["scope"], "allowlisted_workspace_files")
        reads = [json.loads(message["content"]) for message in model.call_args_list[2].args[0]["messages"][-2:]]
        self.assertEqual([result["content"] for result in reads], [code, test_content])
        self.assertTrue(all(result["ok"] for result in reads))
        tested, ran = [json.loads(message["content"]) for message in model.call_args_list[-1].args[0]["messages"][-2:]]
        self.assertTrue(tested["ok"])
        self.assertEqual(tested["exit_code"], 0)
        self.assertIn("Ran 4 tests", tested["stderr"])
        self.assertIn("OK", tested["stderr"])
        self.assertTrue(ran["ok"])
        self.assertEqual(ran["exit_code"], 0)
        self.assertEqual(ran["stdout"].strip(), "5050")
        self.assertEqual(ran["stderr"], "")
        self.assertEqual((self.workspace / "main.py").read_text(encoding="utf-8"), code)
        self.assertEqual((self.workspace / "test_main.py").read_text(encoding="utf-8"), test_content)
        entries = [json.loads(line) for line in log_file.getvalue().splitlines()]
        decisions = [entry for entry in entries if entry["event"] == "tool_approval"]
        self.assertEqual([entry["source"] for entry in decisions], ["auto_read"] * 3 + ["user"] * 2)

    def test_truncated_summary_preserves_successful_execution_results(self):
        code = "def sum_to(n):\n    return sum(range(1, n + 1))\n\nif __name__ == '__main__':\n    print(sum_to(100))\n"
        (self.workspace / "main.py").write_text(code, encoding="utf-8")
        fixture = Path(tool_call.__file__).parent / "workspace" / "test_main.py"
        test_content = fixture.read_bytes()
        (self.workspace / "test_main.py").write_bytes(test_content)
        running = copy.deepcopy(self.response)
        running["choices"][0]["message"]["tool_calls"] = [
            {"id": call_id, "type": "function", "function": {"name": "run_python", "arguments": json.dumps({"path": path})}}
            for call_id, path in [("call_tests", "test_main.py"), ("call_main", "main.py")]
        ]
        truncated = {"choices": [{"finish_reason": "length", "message": {
            "content": "四个测试通过，主程序输出 5050。补充说明尚未完成",
            "tool_calls": [{"id": "call_incomplete_write", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "main.py", "content": "raise RuntimeError()\n"}),
            }}],
        }}]}
        log_dir = self.workspace / "logs"
        with patch.object(tool_call, "LOG_DIR", log_dir), patch.object(sys, "argv", ["tool_call.py", "--auto-read", "验证现有脚本"]):
            with patch("tool_call.get_api_key", return_value="offline-placeholder"):
                with patch("tool_call.call_model", side_effect=[running, truncated]) as model, patch("tool_call.write_file") as writer:
                    with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()) as output:
                        tool_call.main()
        self.assertEqual(model.call_count, 2)
        self.assertEqual([call.args[0]["max_tokens"] for call in model.call_args_list], [2048, 2048])
        self.assertEqual(approval.call_count, 2)
        writer.assert_not_called()
        entries = [json.loads(line) for line in next(log_dir.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        results = [entry["result"] for entry in entries if entry["event"] == "tool_result"]
        self.assertEqual([result["path"] for result in results], ["test_main.py", "main.py"])
        self.assertTrue(all(result["ok"] and result["exit_code"] == 0 for result in results))
        self.assertIn("Ran 4 tests", results[0]["stderr"])
        self.assertIn("OK", results[0]["stderr"])
        self.assertEqual(results[1]["stdout"].strip(), "5050")
        self.assertEqual(entries[-1]["event"], "run_end")
        self.assertEqual(entries[-1]["reason"], "abnormal_finish")
        self.assertIn("此前已完成的操作和结果保留", output.getvalue())
        self.assertNotIn("[助手最终回答]", output.getvalue())
        self.assertEqual((self.workspace / "main.py").read_text(encoding="utf-8"), code)
        self.assertEqual((self.workspace / "test_main.py").read_bytes(), test_content)

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
        for path in ("main.py", "test_main.py"):
            with self.subTest(path=path):
                self.response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps({"path": path})
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
        runner_schema = next(item["function"] for item in payload["tools"] if item["function"]["name"] == "run_python")
        self.assertEqual(runner_schema["parameters"]["properties"]["path"]["enum"], ["main.py", "test_main.py"])
        self.assertFalse(log_dir.exists())
        self.assertFalse((self.workspace / "main.py").exists())
        key.assert_not_called()
        model.assert_not_called()
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
