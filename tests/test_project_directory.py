import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call
from learn_agent import permissions, tools


class ProjectDirectoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.default = self.root / "课程 workspace"
        (self.default / "demo_project").mkdir(parents=True)
        (self.default / "test_main.py").write_text("fixed test\n", encoding="utf-8")
        (self.default / "demo_project/test_stats.py").write_text("fixed nested test\n", encoding="utf-8")
        self.project = self.root / "我的 项目"
        (self.project / "src").mkdir(parents=True)
        (self.project / "src/calc.py").write_text("def total(values):\n    return sum(values)\n", encoding="utf-8")
        self.code = "from src.calc import total\nvalues = [1, 2, 3]\nprint(total(values))\n"
        (self.project / "main.py").write_text(self.code, encoding="utf-8")
        self.logs = self.root / "logs"
        for module, name, value in [(permissions, "WORKSPACE", self.default),
                                    (permissions, "DEFAULT_WORKSPACE", self.default),
                                    (tool_call, "LOG_DIR", self.logs)]:
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.final = {"choices": [{"finish_reason": "stop", "message": {"content": "完成。"}}]}

    def run_cli(self, arguments, replies=()):
        with patch.object(sys, "argv", ["tool_call.py", *arguments]):
            with patch("tool_call.get_api_key", return_value="offline-placeholder") as key:
                with patch("learn_agent.loop.call_model", side_effect=replies) as model:
                    with patch("builtins.input", return_value="y") as approval:
                        with contextlib.redirect_stdout(io.StringIO()) as output:
                            tool_call.main()
        return key, model, approval, output.getvalue()

    def response(self, calls):
        return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for call_id, name, arguments in calls
        ]}}]}

    def saved_history(self, workspace):
        start = {"event": "run_start"}
        if workspace is not None:
            start["workspace"] = str(workspace)
        entries = [start,
                   {"event": "model_request", "step": 1, "payload": {"messages": [
                       {"role": "system", "content": "old"}, {"role": "user", "content": "历史任务"}]}},
                   {"event": "model_response", "step": 1, "response": self.final},
                   {"event": "run_end", "reason": "stop"}]
        path = self.root / "history.jsonl"
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
        return path

    def test_preview_selects_absolute_root_without_key_network_or_tools(self):
        with patch("learn_agent.tools.read_file") as reader, patch("learn_agent.tools.write_file") as writer:
            key, model, approval, output = self.run_cli(["--workspace", str(self.project), "--preview", "浏览项目"])
        payload, _ = json.JSONDecoder().raw_decode(output.split("\n", 1)[1])
        self.assertIn(str(self.project.resolve()), payload["messages"][0]["content"])
        self.assertIn("无课程固定只读文件", payload["messages"][0]["content"])
        self.assertEqual(permissions.WORKSPACE, self.default)
        for operation in (key, model, approval, reader, writer):
            operation.assert_not_called()
        self.assertFalse(self.logs.exists())

    def test_relative_selection_uses_terminal_directory_and_default_stays_fixed(self):
        with contextlib.chdir(self.root):
            _, _, _, selected = self.run_cli(["--workspace", self.project.name, "--preview"])
        with contextlib.chdir(self.project):
            _, _, _, default = self.run_cli(["--preview"])
        selected_payload, _ = json.JSONDecoder().raw_decode(selected.split("\n", 1)[1])
        default_payload, _ = json.JSONDecoder().raw_decode(default.split("\n", 1)[1])
        self.assertIn(str(self.project.resolve()), selected_payload["messages"][0]["content"])
        self.assertIn("只阅读", selected_payload["messages"][-1]["content"])
        self.assertIn(str(self.default.resolve()), default_payload["messages"][0]["content"])
        self.assertIn("report.md", default_payload["messages"][-1]["content"])

    def test_path_examples_are_relative_to_each_selected_root(self):
        for root in (self.default, self.default / "demo_project", self.project):
            with self.subTest(root=root):
                key, model, approval, output = self.run_cli(["--workspace", str(root), "--preview", "运行根目录脚本"])
                payload, _ = json.JSONDecoder().raw_decode(output.split("\n", 1)[1])
                instruction = payload["messages"][0]["content"]
                self.assertIn(str(root.resolve()), instruction)
                self.assertIn('{"path": "main.py"}', instruction)
                self.assertIn('{"path": "src/stats.py"}', instruction)
                self.assertNotIn("例如 demo_project/", instruction)
                self.assertNotIn("demo_project/", json.dumps(payload["tools"], ensure_ascii=False))
                for operation in (key, model, approval):
                    operation.assert_not_called()
                self.assertFalse(self.logs.exists())

    def test_run_python_from_selected_nested_project_keeps_exact_relative_path(self):
        project = self.default / "demo_project"
        (project / "main.py").write_text("print(5050)\n", encoding="utf-8")
        replies = [
            self.response([("wrong-prefix", "run_python", {"path": "demo_project/main.py"})]),
            self.response([("correct-path", "run_python", {"path": "main.py"})]),
            self.final,
        ]
        _, model, approval, _ = self.run_cli(["--workspace", str(project), "运行根目录 main.py"], replies)
        entries = [json.loads(line) for line in next(self.logs.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        results = [entry["result"] for entry in entries if entry["event"] == "tool_result"]
        self.assertEqual(results[0]["path"], "demo_project/main.py")
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"]["type"], "FileNotFoundError")
        self.assertTrue(results[1]["ok"])
        self.assertEqual(results[1]["path"], "main.py")
        self.assertEqual(results[1]["stdout"], "5050\n")
        self.assertEqual((model.call_count, approval.call_count), (3, 2))
        self.assertEqual((entries[-1]["reason"], entries[-1]["task_state"]), ("stop", "completed"))

    def test_invalid_root_stops_before_credentials_network_or_log_creation(self):
        for selected in ("", str(self.root / "missing"), str(self.project / "main.py")):
            with self.subTest(selected=selected), patch.object(sys, "argv", ["tool_call.py", "--workspace", selected]):
                with patch("tool_call.get_api_key") as key, patch("learn_agent.loop.call_model") as model:
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                        tool_call.main()
                key.assert_not_called()
                model.assert_not_called()
                self.assertEqual(permissions.WORKSPACE, self.default)
                self.assertFalse(self.logs.exists())

    def test_all_six_tools_follow_selected_root_and_record_approvals(self):
        replies = [
            self.response([("list", "list_files", {}), ("search", "search_text", {"query": "def total", "path": "src"})]),
            self.response([("read-main", "read_file", {"path": "main.py"}), ("read-calc", "read_file", {"path": "src/calc.py"})]),
            self.response([("write", "write_file", {"path": "report.md", "content": "项目统计验证"}),
                           ("edit", "edit_file", {"path": "main.py", "old_text": "[1, 2, 3]", "new_text": "[2, 4, 6]"})]),
            self.response([("run", "run_python", {"path": "main.py"})]), self.final,
        ]
        key, model, approval, _ = self.run_cli(["--workspace", str(self.project), "--auto-read", "验证工具"], replies)
        self.assertEqual(model.call_count, 5)
        key.assert_called_once()
        self.assertEqual(approval.call_count, 3)
        self.assertEqual((self.project / "main.py").read_text(encoding="utf-8"), self.code.replace("[1, 2, 3]", "[2, 4, 6]"))
        self.assertEqual((self.project / "report.md").read_text(encoding="utf-8"), "项目统计验证")
        self.assertFalse((self.default / "report.md").exists())
        entries = [json.loads(line) for line in next(self.logs.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(entries[0]["workspace"], str(self.project.resolve()))
        results = {entry["tool_call_id"]: entry["result"] for entry in entries if entry["event"] == "tool_result"}
        self.assertEqual(results["list"]["files"], ["main.py", "src/"])
        self.assertEqual(results["search"]["matches"][0]["path"], "src/calc.py")
        self.assertEqual(results["read-main"]["content"], self.code)
        self.assertEqual(results["run"]["stdout"], "12\n")
        self.assertTrue(all(result["ok"] for result in results.values()))
        decisions = [entry["source"] for entry in entries if entry["event"] == "tool_approval"]
        self.assertEqual(decisions, ["auto_read"] * 4 + ["user"] * 3)
        self.assertEqual(entries[-1]["reason"], "stop")
        self.assertEqual(permissions.WORKSPACE, self.default)

    def test_fixed_files_stay_protected_when_selecting_their_subdirectory(self):
        with permissions.use_workspace(self.default / "demo_project"):
            self.assertEqual(permissions.read_only_paths(), ["test_stats.py"])
            with self.assertRaises(ValueError):
                tools.write_file("test_stats.py", "changed")
        self.assertEqual((self.default / "demo_project/test_stats.py").read_text(encoding="utf-8"), "fixed nested test\n")

    def test_unrelated_project_can_edit_files_with_same_fixture_names(self):
        with permissions.use_workspace(self.project):
            self.assertEqual(permissions.read_only_paths(), [])
            tools.write_file("test_main.py", "project test\n")
            tools.write_file("project.txt", "project data\n")
        self.assertEqual((self.project / "test_main.py").read_text(encoding="utf-8"), "project test\n")
        self.assertEqual((self.default / "test_main.py").read_text(encoding="utf-8"), "fixed test\n")

    def test_path_checks_block_leaving_selected_project(self):
        with permissions.use_workspace(self.project):
            for path in ("../课程 workspace/test_main.py", str(self.default / "test_main.py"), ".env", "src/../main.py"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    permissions.resolve_workspace_path(path)
            resolve = Path.resolve

            def outside(path, *args, **kwargs):
                if path == self.project / "alias.py":
                    return self.default / "test_main.py"
                return resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", autospec=True, side_effect=outside), self.assertRaises(ValueError):
                tools.read_file("alias.py")

    def test_workspace_is_restored_after_request_failure(self):
        with patch.object(sys, "argv", ["tool_call.py", "--workspace", str(self.project), "测试失败"]):
            with patch("tool_call.get_api_key", return_value="offline-placeholder"):
                with patch("learn_agent.loop.call_model", side_effect=OSError("offline")):
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                        tool_call.main()
        self.assertEqual(permissions.WORKSPACE, self.default)
        entries = [json.loads(line) for line in next(self.logs.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(entries[0]["workspace"], str(self.project.resolve()))
        self.assertEqual(entries[-1]["reason"], "error")

    def test_resume_same_project_keeps_root_and_history(self):
        source = self.saved_history(self.project)
        original = source.read_bytes()
        _, model, _, _ = self.run_cli(["--workspace", str(self.project / "src/.."), "--resume", str(source), "继续"], [self.final])
        request = model.call_args.args[0]
        self.assertIn(str(self.project.resolve()), request["messages"][0]["content"])
        self.assertEqual(request["messages"][1]["content"], "历史任务")
        self.assertEqual(source.read_bytes(), original)
        entries = [json.loads(line) for line in next(self.logs.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(entries[0]["workspace"], str(self.project.resolve()))

    def test_resume_other_project_is_rejected_before_credentials_or_network(self):
        source = self.saved_history(self.project)
        with patch.object(sys, "argv", ["tool_call.py", "--resume", str(source), "继续"]):
            with patch("tool_call.get_api_key") as key, patch("learn_agent.loop.call_model") as model:
                with self.assertRaises(SystemExit):
                    tool_call.main()
        key.assert_not_called()
        model.assert_not_called()
        self.assertFalse(self.logs.exists())

    def test_legacy_history_without_root_only_resumes_in_default_workspace(self):
        source = self.saved_history(None)
        self.run_cli(["--preview", "--resume", str(source), "继续"])
        with self.assertRaises(SystemExit):
            self.run_cli(["--workspace", str(self.project), "--preview", "--resume", str(source), "继续"])
        self.assertEqual(permissions.WORKSPACE, self.default)

    def test_invalid_workspace_metadata_is_rejected(self):
        source = self.saved_history(self.project)
        entries = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        for recorded in (None, "", "relative/project", 123, []):
            with self.subTest(recorded=recorded):
                entries[0]["workspace"] = recorded
                source.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
                with self.assertRaises(SystemExit):
                    self.run_cli(["--workspace", str(self.project), "--resume", str(source), "继续"])
        self.assertFalse(self.logs.exists())


if __name__ == "__main__":
    unittest.main()
