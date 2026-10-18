import contextlib
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import tool_call
from learn_agent import commands, loop, permissions


class CommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.project = self.root / "命令 项目"
        (self.project / "src").mkdir(parents=True)
        (self.project / "src/calc.py").write_text("def total(values):\n    return sum(values)\n", encoding="utf-8")
        (self.project / "main.py").write_text("from src.calc import total\nprint(total([1, 2, 3]))\n", encoding="utf-8")
        (self.project / "test_calc.py").write_text(
            "import unittest\nfrom src.calc import total\n\nclass TestTotal(unittest.TestCase):\n"
            "    def test_empty(self): self.assertEqual(total([]), 0)\n"
            "    def test_single(self): self.assertEqual(total([7]), 7)\n"
            "    def test_regular(self): self.assertEqual(total([1, 2, 3]), 6)\n"
            "    def test_negative(self): self.assertEqual(total([-1, -2]), -3)\n", encoding="utf-8")
        workspace = patch.object(permissions, "WORKSPACE", self.project)
        workspace.start()
        self.addCleanup(workspace.stop)
        self.payload = {"messages": [{"role": "user", "content": "执行测试命令"}]}
        self.final = {"choices": [{"finish_reason": "stop", "message": {"content": "根据实际结果回答。"}}]}

    def response(self, calls):
        return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for call_id, name, arguments in calls
        ]}}]}

    def test_runs_unittest_module_in_selected_project(self):
        result = commands.run_command(["python", "-m", "unittest", "-v", "test_calc.py"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["command"][0], sys.executable)
        self.assertEqual(result["cwd"], str(self.project.resolve()))
        self.assertIn("Ran 4 tests", result["stderr"])
        self.assertIn("OK", result["stderr"])
        self.assertEqual(result["stdout"], "")
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["stderr_truncated"])
        self.assertFalse(list(self.project.rglob("__pycache__")))

    def test_captures_chinese_and_removes_key_and_closes_stdin(self):
        code = "import os,sys; print('中文输出'); print(os.getenv('DEEPSEEK_API_KEY')); print(repr(sys.stdin.read())); print('错误流', file=sys.stderr)"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline-private-key"}):
            result = commands.run_command(["python", "-c", code])
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"].splitlines(), ["中文输出", "None", "''"])
        self.assertEqual(result["stderr"].strip(), "错误流")
        self.assertNotIn("offline-private-key", json.dumps(result))

    def test_argument_boundaries_preserve_spaces_and_shell_symbols(self):
        arguments = ["中文 空格", "& echo injected", "a|b", "> marker.txt", "", 'quote"value']
        result = commands.run_command(["python", "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *arguments])
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(result["stdout"]), arguments)
        self.assertFalse((self.project / "marker.txt").exists())

    def test_uses_selected_subdirectory_as_cwd(self):
        result = commands.run_command(["python", "-c", "from pathlib import Path; print(Path.cwd().name)"], path="src")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"].strip(), "src")
        self.assertEqual(result["cwd"], str((self.project / "src").resolve()))

    def test_nonzero_exit_preserves_real_output(self):
        result = commands.run_command(["python", "-c", "import sys; print('before'); print('failure',file=sys.stderr); sys.exit(7)"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["stdout"].strip(), "before")
        self.assertEqual(result["stderr"].strip(), "failure")
        self.assertFalse(result["timed_out"])

    def test_failed_unittest_returns_failure_report(self):
        (self.project / "src/calc.py").write_text("def total(values):\n    return -999\n", encoding="utf-8")
        result = commands.run_command(["python", "-m", "unittest", "-q", "test_calc.py"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("Ran 4 tests", result["stderr"])
        self.assertIn("FAILED (failures=4)", result["stderr"])

    def test_missing_program_returns_start_error(self):
        result = commands.run_command(["learn-agent-program-that-does-not-exist-7391"])
        self.assertFalse(result["ok"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["error"]["type"], "FileNotFoundError")

    def test_invalid_arguments_block_execution_before_confirmation(self):
        cases = [{"command": value} for value in ("python -m unittest", [], [""], [123], ["python", None], ["python\x00"])]
        cases += [{"command": ["python"], "timeout": value} for value in (True, 0, 121, 0.5)]
        cases += [{"command": ["python"], "path": path} for path in ("../outside", str(self.root), ".git", "main.py")]
        cases += [{"command": ["python"], "extra": True}, {"command": ["script.cmd"]}]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                reply = self.response([("invalid", "run_command", arguments)])
                with patch("learn_agent.loop.call_model", return_value=reply), patch("learn_agent.commands.subprocess.Popen") as process:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                        loop.run_agent(self.payload, "offline-placeholder", auto_read=True)
                process.assert_not_called()
                approval.assert_not_called()

    def test_batch_wrapper_resolved_from_path_is_rejected(self):
        with patch("learn_agent.commands.shutil.which", return_value=str(self.project / "wrapper.cmd")):
            with self.assertRaises(ValueError):
                commands.validate_command(["wrapper"])

    def test_timeout_preserves_partial_output_and_cleans_up(self):
        result = commands.run_command(["python", "-c", "import time; print('started',flush=True); time.sleep(30)"], timeout=1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["error"]["type"], "TimeoutExpired")
        self.assertIn("started", result["stdout"])
        self.assertTrue(result["cleanup_succeeded"])

    def test_output_limit_stops_running_command_and_bounds_both_streams(self):
        code = "import sys,time; sys.stdout.write('a'*5000); sys.stdout.flush(); sys.stderr.write('b'*5000); sys.stderr.flush(); time.sleep(30)"
        with patch.object(commands, "MAX_OUTPUT_BYTES", 1024):
            result = commands.run_command(["python", "-c", code])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "OutputLimitExceeded")
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), 1024)
        self.assertLessEqual(len(result["stderr"].encode("utf-8")), 1024)
        self.assertTrue(result["stdout_truncated"])
        self.assertTrue(result["stderr_truncated"])
        self.assertTrue(result["cleanup_succeeded"])

    def test_fast_command_with_excess_output_is_still_incomplete(self):
        with patch.object(commands, "MAX_OUTPUT_BYTES", 1024):
            result = commands.run_command(["python", "-c", "print('a'*5000)"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["stdout_truncated"])
        self.assertEqual(result["error"]["type"], "OutputLimitExceeded")

    @unittest.skipUnless(os.name == "nt", "Windows process-tree cleanup")
    def test_windows_timeout_terminates_waiting_command_and_child(self):
        code = "import subprocess,sys; from pathlib import Path; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); Path('child.pid').write_text(str(child.pid)); child.wait()"
        result = commands.run_command(["python", "-c", code], timeout=1)
        pid = int((self.project / "child.pid").read_text())
        taskkill = str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe")
        child_stopped = False
        try:
            self.assertTrue(result["cleanup_succeeded"])
            tasklist = str(Path(os.environ["SystemRoot"]) / "System32/tasklist.exe")
            snapshot = subprocess.run([tasklist, "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(snapshot.returncode, 0)
            child_stopped = not any(len(row) > 1 and row[1] == str(pid) for row in csv.reader(io.StringIO(snapshot.stdout)))
            self.assertTrue(child_stopped)
        finally:
            if not child_stopped:
                subprocess.run([taskkill, "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_interrupt_cleans_up_and_is_not_reported_as_command_failure(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = KeyboardInterrupt()
        with patch("learn_agent.commands.subprocess.Popen", return_value=process):
            with patch("learn_agent.commands.stop_process_tree", return_value=True) as cleanup:
                with self.assertRaises(KeyboardInterrupt):
                    commands.run_command(["python", "-c", "print(1)"])
        cleanup.assert_called_once_with(process)

    def test_cleanup_failure_is_reported_without_claiming_process_exit(self):
        process = Mock(returncode=None)
        process.poll.return_value = None
        with patch("learn_agent.commands.subprocess.Popen", return_value=process):
            with patch("learn_agent.commands.time.monotonic", side_effect=[0, 2]):
                with patch("learn_agent.commands.stop_process_tree", return_value=False):
                    result = commands.run_command(["python", "-c", "print(1)"], timeout=1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["cleanup_succeeded"])
        self.assertIsNone(result["exit_code"])

    def test_auto_read_still_requires_confirmation_and_denial_stops(self):
        reply = self.response([("command", "run_command", {"command": ["python", "main.py"]})])
        log = io.StringIO()
        with patch("learn_agent.loop.call_model", return_value=reply) as model, patch("learn_agent.commands.subprocess.Popen") as process:
            with patch("builtins.input", return_value="n") as approval, contextlib.redirect_stdout(io.StringIO()):
                reason = loop.run_agent(self.payload, "offline-placeholder", log, auto_read=True)
        self.assertEqual(reason, "user_denied")
        model.assert_called_once()
        process.assert_not_called()
        self.assertIn("main.py", approval.call_args.args[0])
        decision = json.loads(log.getvalue().splitlines()[-1])
        self.assertEqual((decision["source"], decision["approved"]), ("user", False))

    def test_invalid_later_call_blocks_entire_batch(self):
        reply = self.response([("valid", "run_command", {"command": ["python", "main.py"]}),
                               ("invalid", "run_command", {"command": ["python"], "path": "../outside"})])
        with patch("learn_agent.loop.call_model", return_value=reply), patch("learn_agent.commands.subprocess.Popen") as process:
            with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                loop.run_agent(self.payload, "offline-placeholder", auto_read=True)
        process.assert_not_called()
        approval.assert_not_called()

    def test_cli_records_real_test_results_and_both_confirmations(self):
        reply = self.response([("tests", "run_command", {"command": ["python", "-m", "unittest", "-v", "test_calc.py"]}),
                               ("main", "run_command", {"command": ["python", "main.py"]})])
        logs = self.root / "logs"
        with patch.object(sys, "argv", ["tool_call.py", "--workspace", str(self.project), "--auto-read", "验证"]):
            with patch.object(tool_call, "LOG_DIR", logs), patch("tool_call.get_api_key", return_value="offline-placeholder"):
                with patch("learn_agent.loop.call_model", side_effect=[reply, self.final]) as model:
                    with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                        tool_call.main()
        self.assertEqual(approval.call_count, 2)
        history = model.call_args_list[1].args[0]["messages"]
        results = [json.loads(message["content"]) for message in history if message["role"] == "tool"]
        self.assertIn("Ran 4 tests", results[0]["stderr"])
        self.assertEqual(results[1]["stdout"], "6\n")
        self.assertTrue(all(result["ok"] for result in results))
        entries = [json.loads(line) for line in next(logs.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([entry["result"] for entry in entries if entry["event"] == "tool_result"], results)
        self.assertEqual([entry["source"] for entry in entries if entry["event"] == "tool_approval"], ["user", "user"])
        self.assertEqual(entries[-1]["reason"], "stop")

    def test_preview_advertises_command_without_execution_key_or_log(self):
        logs = self.root / "logs"
        with patch.object(sys, "argv", ["tool_call.py", "--workspace", str(self.project), "--preview"]):
            with patch.object(tool_call, "LOG_DIR", logs), patch("tool_call.get_api_key") as key:
                with patch("learn_agent.loop.call_model") as model, patch("learn_agent.commands.subprocess.Popen") as process:
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        tool_call.main()
        payload, _ = json.JSONDecoder().raw_decode(output.getvalue().split("\n", 1)[1])
        schema = next(item["function"] for item in payload["tools"] if item["function"]["name"] == "run_command")
        self.assertEqual(schema["parameters"]["properties"]["command"]["type"], "array")
        for operation in (key, model, process):
            operation.assert_not_called()
        self.assertFalse(logs.exists())


if __name__ == "__main__":
    unittest.main()
