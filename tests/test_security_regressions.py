"""Real temporary-file/process checks; approved execution is explicitly not an OS sandbox."""

import contextlib
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from learn_agent import commands, loop, permissions, tools


class SecurityRegressionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-security-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.project = self.root / "project"
        self.outside = self.root / "outside"
        self.project.mkdir()
        self.outside.mkdir()
        (self.outside / "private.txt").write_text("FAKE-OUTSIDE-DATA", encoding="utf-8")
        (self.outside / "main.py").write_text("print('outside')\n", encoding="utf-8")
        workspace = patch.object(permissions, "WORKSPACE", self.project)
        workspace.start()
        self.addCleanup(workspace.stop)

    def link(self, alias, target):
        if os.name == "nt":
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(target)],
                                    capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            alias.symlink_to(target, target_is_directory=True)

    def unlink(self, alias):
        alias.rmdir() if os.name == "nt" else alias.unlink()

    def test_real_directory_link_cannot_expose_outside_files_to_file_tools(self):
        alias = self.project / "alias"
        self.link(alias, self.outside)
        self.addCleanup(self.unlink, alias)
        calls = [
            ("read_file", {"path": "alias/private.txt"}),
            ("list_files", {"path": "alias"}),
            ("search_text", {"path": "alias", "query": "FAKE"}),
            ("write_file", {"path": "alias/private.txt", "content": "changed"}),
            ("edit_file", {"path": "alias/private.txt", "old_text": "FAKE", "new_text": "changed"}),
            ("run_python", {"path": "alias/main.py"}),
        ]
        for name, arguments in calls:
            with self.subTest(tool=name), self.assertRaises(ValueError):
                tools.TOOL_REGISTRY[name].validate_arguments(arguments)
        self.assertNotIn("alias/", tools.list_files())
        self.assertEqual((self.outside / "private.txt").read_text(), "FAKE-OUTSIDE-DATA")

    def test_directory_link_changed_after_preview_does_not_redirect_a_write(self):
        inside = self.project / "inside"
        inside.mkdir()
        alias = self.project / "alias"
        self.link(alias, inside)
        self.addCleanup(self.unlink, alias)
        with contextlib.redirect_stdout(io.StringIO()):
            prepared = tools.TOOL_REGISTRY["write_file"].prepare({"path": "alias/new.txt", "content": "allowed preview"})
        self.unlink(alias)
        self.link(alias, self.outside)
        with self.assertRaises(ValueError):
            prepared.execute()
        self.assertFalse((inside / "new.txt").exists())
        self.assertFalse((self.outside / "new.txt").exists())

    def test_approved_code_is_not_an_os_sandbox_but_child_environment_omits_model_key(self):
        (self.project / "probe.py").write_text(
            "import json,os\nfrom pathlib import Path\n"
            "print(json.dumps({'outside': Path('../outside/private.txt').read_text(), "
            "'key_present': 'DEEPSEEK_API_KEY' in os.environ}))\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            tools.read_file("../outside/private.txt")
        for name, arguments in [("run_python", {"path": "probe.py"}),
                                ("run_command", {"command": ["python", "probe.py"]})]:
            with self.subTest(tool=name), patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline-security-placeholder"}):
                result = tools.TOOL_REGISTRY[name].prepare(arguments).execute()
                self.assertTrue(result["ok"])
                self.assertEqual(json.loads(result["stdout"]), {"outside": "FAKE-OUTSIDE-DATA", "key_present": False})
                self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "offline-security-placeholder")
                self.assertNotIn("offline-security-placeholder", json.dumps(result))

    def test_run_python_excess_output_is_bounded_even_when_script_exits_zero(self):
        (self.project / "noisy.py").write_text("print('x' * 50000)\n", encoding="utf-8")
        result = tools.TOOL_REGISTRY["run_python"].prepare({"path": "noisy.py"}).execute()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "OutputLimitExceeded")
        self.assertTrue(result["stdout_truncated"])
        self.assertLessEqual(len(result["stdout"].encode()), commands.MAX_OUTPUT_BYTES)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree cleanup")
    def test_run_python_timeout_stops_its_child_process(self):
        (self.project / "child.py").write_text(
            "import os,time\nfrom pathlib import Path\n"
            "Path('child.pid').write_text(str(os.getpid()))\ntime.sleep(30)\n", encoding="utf-8")
        (self.project / "main.py").write_text(
            "import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-B','child.py'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\ntime.sleep(30)\n",
            encoding="utf-8")
        pid_file = self.project / "child.pid"
        child_stopped = False
        try:
            with patch.object(tools, "RUN_TIMEOUT", 1):
                result = tools.TOOL_REGISTRY["run_python"].prepare({"path": "main.py"}).execute()
            pid = int(pid_file.read_text())
            tasklist = Path(os.environ["SystemRoot"]) / "System32/tasklist.exe"
            snapshot = subprocess.run([str(tasklist), "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                      capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
            alive = any(len(row) > 1 and row[1] == str(pid) for row in csv.reader(io.StringIO(snapshot.stdout)))
            child_stopped = not alive
            self.assertFalse(alive, "the owned child survived the run_python timeout")
            self.assertFalse(result["ok"])
            self.assertTrue(result["timed_out"])
            self.assertTrue(result["cleanup_succeeded"])
        finally:
            if pid_file.exists():
                pid = int(pid_file.read_text())
                taskkill = Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"
                if not child_stopped:
                    subprocess.run([str(taskkill), "/PID", str(pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)

    def test_uncertain_python_cleanup_stops_the_remaining_batch_and_model_calls(self):
        (self.project / "main.py").write_text("print(42)\n", encoding="utf-8")
        calls = [{"id": f"call-{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
                 for index, (name, arguments) in enumerate([
                     ("run_python", {"path": "main.py"}), ("write_file", {"path": "unexpected.txt", "content": "must not run"})])]
        reply = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": calls}}]}
        failure = {"ok": False, "exit_code": None, "stdout": "", "stderr": "", "timed_out": True,
                   "cleanup_succeeded": False, "error": {"type": "TimeoutExpired", "message": "test cleanup failure"}}
        log = io.StringIO()
        with patch("learn_agent.providers.call_model", return_value=reply) as model:
            with patch.object(commands, "run_command", return_value=failure), patch("builtins.input", return_value="y") as approval:
                with contextlib.redirect_stdout(io.StringIO()):
                    reason = loop.run_agent({"messages": [{"role": "user", "content": "test"}]}, "offline-key", log)
        self.assertEqual(reason, "error")
        model.assert_called_once()
        approval.assert_called_once()
        self.assertFalse((self.project / "unexpected.txt").exists())
        events = [json.loads(line) for line in log.getvalue().splitlines()]
        self.assertEqual([e["tool"] for e in events if e["event"] == "tool_started"], ["run_python"])


if __name__ == "__main__":
    unittest.main()
