import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call
from learn_agent import loop, permissions, tools


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="learn-agent-test-")
        self.root = Path(self.directory.name)
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.directory.cleanup)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        workspace_patch = patch.object(permissions, "WORKSPACE", self.workspace)
        workspace_patch.start()
        self.addCleanup(workspace_patch.stop)
        fixture_patch = patch.object(permissions, "DEFAULT_WORKSPACE", self.workspace)
        fixture_patch.start()
        self.addCleanup(fixture_patch.stop)
        self.sources = {
            "demo/main.py": "from src.stats import summarize\nprint(summarize([12, 18, 30]))\n",
            "demo/src/stats.py": 'def summarize(values):\n    return {"total": sum(values)}\n',
        }
        for name, content in self.sources.items():
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.payload = {"messages": [{"role": "system", "content": "test"}, {"role": "user", "content": "了解项目"}]}
        self.final = {"choices": [{"finish_reason": "stop", "message": {"content": "已根据源码回答。"}}]}

    def response(self, calls):
        return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for call_id, name, arguments in calls
        ]}}]}

    def test_lists_one_level_and_reads_nested_utf8_source(self):
        self.assertEqual(tools.list_files(), ["demo/"])
        self.assertEqual(tools.list_files("demo"), ["main.py", "src/"])
        self.assertEqual(tools.list_files("demo/src"), ["stats.py"])
        for name, content in self.sources.items():
            with self.subTest(path=name):
                self.assertEqual(tools.read_file(name), content)

    def test_rejects_absolute_parent_hidden_and_invalid_paths(self):
        paths = ["../outside.txt", "demo/../../outside.txt", str(self.root / "outside.txt"),
                 "C:relative.txt", "main.py:stream", ".env", "demo/.git/config", "", None, True, []]
        for path in paths:
            for operation in (tools.read_file, tools.list_files, permissions.resolve_write_path, permissions.resolve_python_path):
                with self.subTest(path=path, operation=operation.__name__), self.assertRaises(ValueError):
                    operation(path)

    def test_dot_files_and_directories_are_not_listed(self):
        (self.workspace / ".env").write_text("offline-private-value", encoding="utf-8")
        (self.workspace / ".git").mkdir()
        (self.workspace / "notes.txt").write_text("公开资料", encoding="utf-8")
        self.assertEqual(tools.list_files(), ["demo/", "notes.txt"])

    def test_resolved_paths_outside_workspace_are_rejected_and_not_listed(self):
        alias = self.workspace / "external.py"
        alias.write_text("alias placeholder", encoding="utf-8")
        outside = self.root / "outside.py"
        outside.write_text("outside content", encoding="utf-8")
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            return outside if path == alias else original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            with self.assertRaises(ValueError):
                tools.read_file("external.py")
            with self.assertRaises(ValueError):
                tools.list_files("external.py")
            with self.assertRaises(ValueError):
                tools.write_file("external.py", "overwritten")
            with patch("learn_agent.commands.subprocess.Popen") as process, self.assertRaises(ValueError):
                tools.run_python("external.py")
            process.assert_not_called()
            self.assertEqual(tools.list_files(), ["demo/"])
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside content")

    def test_agent_browses_subdirectories_then_reads_sources(self):
        replies = [
            self.response([("call_project", "list_files", {"path": "demo"})]),
            self.response([("call_src", "list_files", {"path": "demo/src"}), ("call_main", "read_file", {"path": "demo/main.py"})]),
            self.response([("call_stats", "read_file", {"path": "demo/src/stats.py"})]),
            self.final,
        ]
        with patch("learn_agent.providers.call_model", side_effect=replies) as model:
            with patch("learn_agent.tools.write_file") as writer, patch("learn_agent.tools.run_python") as runner:
                with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                    reason = loop.run_agent(self.payload, "offline-placeholder", auto_read=True)
        self.assertEqual(reason, "stop")
        self.assertEqual(model.call_count, 4)
        for operation in (writer, runner, approval):
            operation.assert_not_called()
        history = model.call_args_list[-1].args[0]["messages"]
        results = [json.loads(message["content"]) for message in history if message["role"] == "tool"]
        self.assertEqual(results[0]["path"], "demo")
        self.assertEqual(results[0]["scope"], "visible_workspace_directory")
        self.assertEqual(results[0]["files"], ["main.py", "src/"])
        self.assertEqual(results[1]["files"], ["stats.py"])
        self.assertEqual([result["content"] for result in results[2:]], list(self.sources.values()))
        self.assertTrue(all(result["ok"] for result in results))
        for name, content in self.sources.items():
            self.assertEqual((self.workspace / name).read_text(encoding="utf-8"), content)

    def test_invalid_parameters_are_rejected_before_confirmation(self):
        for name, arguments in [
            ("read_file", {"path": "../outside.txt"}), ("read_file", {"path": ".env"}),
            ("read_file", {"path": None}), ("read_file", {"path": "demo/main.py", "extra": True}),
            ("list_files", {"path": ".."}), ("list_files", {"path": None}),
            ("list_files", {"path": "demo", "recursive": True}),
        ]:
            with self.subTest(tool=name, arguments=arguments):
                with patch("learn_agent.providers.call_model", return_value=self.response([("call_invalid", name, arguments)])):
                    with patch("learn_agent.tools.read_file") as reader, patch("learn_agent.tools.list_files") as lister:
                        with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                            loop.run_agent(self.payload, "offline-placeholder", auto_read=True)
                for operation in (reader, lister, approval):
                    operation.assert_not_called()

    def test_filesystem_errors_are_reported_to_model_with_the_path(self):
        for name, path, error_type in [
            ("read_file", "missing.py", "FileNotFoundError"),
            ("read_file", "demo/src", "FileNotFoundError"),
            ("list_files", "demo/main.py", "NotADirectoryError"),
        ]:
            with self.subTest(tool=name, path=path):
                replies = [self.response([("call_error", name, {"path": path})]), self.final]
                with patch("learn_agent.providers.call_model", side_effect=replies) as model:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                        reason = loop.run_agent(copy.deepcopy(self.payload), "offline-placeholder", auto_read=True)
                approval.assert_not_called()
                self.assertEqual(reason, "stop")
                result = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
                self.assertFalse(result["ok"])
                self.assertEqual(result["path"], path)
                self.assertEqual(result["error"]["type"], error_type)

    def test_agent_modifies_imported_module_and_verifies_with_fixed_tests(self):
        fixture = Path(tool_call.__file__).parent / "workspace" / "demo_project"
        project = self.workspace / "demo_project"
        initial_stats = ('def summarize(values):\n    count = len(values)\n    total = sum(values)\n'
                         '    return {\n        "count": count,\n        "total": total,\n'
                         '        "average": total / count if count else 0.0,\n    }\n')
        files = {"main.py": self.sources["demo/main.py"], "src/stats.py": initial_stats,
                 "test_stats.py": (fixture / "test_stats.py").read_text(encoding="utf-8")}
        for name, content in files.items():
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        original_tests = (project / "test_stats.py").read_bytes()
        original_main = (project / "main.py").read_bytes()
        original_stats = (project / "src/stats.py").read_text(encoding="utf-8")
        fixed_stats = original_stats.replace(
            '        "average": total / count if count else 0.0,',
            '        "average": total / count if count else 0.0,\n'
            '        "min": min(values) if count else None,\n'
            '        "max": max(values) if count else None,',
        )
        replies = [
            self.response([("call_source", "read_file", {"path": "demo_project/src/stats.py"}),
                           ("call_test_source", "read_file", {"path": "demo_project/test_stats.py"})]),
            self.response([("call_before", "run_python", {"path": "demo_project/test_stats.py"})]),
            self.response([("call_fix", "write_file", {"path": "demo_project/src/stats.py", "content": fixed_stats})]),
            self.response([("call_tests", "run_python", {"path": "demo_project/test_stats.py"}),
                           ("call_main", "run_python", {"path": "demo_project/main.py"})]),
            self.final,
        ]
        log_file = io.StringIO()
        with patch("learn_agent.providers.call_model", side_effect=replies) as model:
            with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                reason = loop.run_agent(self.payload, "offline-placeholder", log_file, auto_read=True)
        self.assertEqual(reason, "stop")
        self.assertEqual(model.call_count, 5)
        self.assertEqual(approval.call_count, 4)
        self.assertIn("覆盖现有 demo_project/src/stats.py", approval.call_args_list[1].args[0])
        history = model.call_args_list[-1].args[0]["messages"]
        results = [json.loads(message["content"]) for message in history if message["role"] == "tool"]
        self.assertEqual([result["ok"] for result in results], [True, True, False, True, True, True])
        self.assertNotEqual(results[2]["exit_code"], 0)
        self.assertIn("FAILED", results[2]["stderr"])
        self.assertEqual(results[4]["exit_code"], 0)
        self.assertIn("Ran 4 tests", results[4]["stderr"])
        self.assertIn("OK", results[4]["stderr"])
        self.assertEqual(results[5]["exit_code"], 0)
        self.assertEqual(results[5]["stdout"].strip(), "{'count': 3, 'total': 60, 'average': 20.0, 'min': 12, 'max': 30}")
        self.assertEqual(results[5]["stderr"], "")
        self.assertEqual((project / "src/stats.py").read_text(encoding="utf-8"), fixed_stats)
        self.assertEqual((project / "test_stats.py").read_bytes(), original_tests)
        self.assertEqual((project / "main.py").read_bytes(), original_main)
        self.assertFalse(list(project.rglob("__pycache__")))
        entries = [json.loads(line) for line in log_file.getvalue().splitlines()]
        self.assertEqual([entry["source"] for entry in entries if entry["event"] == "tool_approval"],
                         ["auto_read", "auto_read", "user", "user", "user", "user"])
        self.assertEqual([entry["result"] for entry in entries if entry["event"] == "tool_result"], results)

    def test_nested_script_imports_project_module_and_reads_local_data(self):
        tools.write_file("demo/input.txt", "项目本地资料")
        tools.write_file("demo/main.py", 'from pathlib import Path\nfrom src.stats import summarize\n'
                             'print(Path("input.txt").read_text(encoding="utf-8"))\nprint(summarize([12, 18, 30]))\n')
        result = tools.run_python("demo/main.py")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].splitlines(), ["项目本地资料", "{'total': 60}"])
        self.assertEqual(result["stderr"], "")

    def test_read_only_fixtures_cannot_be_overwritten_before_confirmation(self):
        for path in (*permissions.READ_ONLY_FILES, "demo_project/./test_stats.py"):
            with self.subTest(path=path):
                reply = self.response([("call_write", "write_file", {"path": path, "content": "skip tests"})])
                with patch("learn_agent.providers.call_model", return_value=reply), patch("learn_agent.tools.write_file") as writer:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                        loop.run_agent(self.payload, "offline-placeholder", auto_read=True)
                writer.assert_not_called()
                approval.assert_not_called()
                with self.assertRaises(ValueError):
                    tools.write_file(path, "skip tests")

    def test_nested_write_and_run_still_require_confirmation(self):
        original = (self.workspace / "demo/main.py").read_bytes()
        for name, arguments in [("write_file", {"path": "demo/main.py", "content": "print(0)\n"}),
                                ("run_python", {"path": "demo/main.py"})]:
            with self.subTest(tool=name):
                log_file = io.StringIO()
                reply = self.response([("call_denied", name, arguments)])
                with patch("learn_agent.providers.call_model", return_value=reply) as model, patch("learn_agent.commands.subprocess.Popen") as process:
                    with patch("builtins.input", return_value="n") as approval, contextlib.redirect_stdout(io.StringIO()):
                        reason = loop.run_agent(self.payload, "offline-placeholder", log_file, auto_read=True)
                self.assertEqual(reason, "user_denied")
                model.assert_called_once()
                approval.assert_called_once()
                process.assert_not_called()
                self.assertEqual((self.workspace / "demo/main.py").read_bytes(), original)
                decision = json.loads(log_file.getvalue().splitlines()[-1])
                self.assertFalse(decision["approved"])
                self.assertEqual(decision["source"], "user")

    def test_missing_parent_is_reported_without_creating_directories(self):
        reply = self.response([("call_missing", "write_file", {"path": "missing/main.py", "content": "print(1)\n"})])
        with patch("learn_agent.providers.call_model", side_effect=[reply, self.final]) as model:
            with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
                reason = loop.run_agent(self.payload, "offline-placeholder", auto_read=True)
        self.assertEqual(reason, "stop")
        result = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "FileNotFoundError")
        self.assertEqual(result["path"], "missing/main.py")
        self.assertFalse((self.workspace / "missing").exists())


if __name__ == "__main__":
    unittest.main()
