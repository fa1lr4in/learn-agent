import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="learn-agent-test-")
        self.root = Path(self.directory.name)
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.directory.cleanup)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        workspace_patch = patch.object(tool_call, "WORKSPACE", self.workspace)
        workspace_patch.start()
        self.addCleanup(workspace_patch.stop)
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
        self.assertEqual(tool_call.list_files(), ["demo/"])
        self.assertEqual(tool_call.list_files("demo"), ["main.py", "src/"])
        self.assertEqual(tool_call.list_files("demo/src"), ["stats.py"])
        for name, content in self.sources.items():
            with self.subTest(path=name):
                self.assertEqual(tool_call.read_file(name), content)

    def test_rejects_absolute_parent_hidden_and_invalid_paths(self):
        paths = ["../outside.txt", "demo/../../outside.txt", str(self.root / "outside.txt"),
                 "C:relative.txt", "main.py:stream", ".env", "demo/.git/config", "", None, True, []]
        for path in paths:
            for operation in (tool_call.read_file, tool_call.list_files):
                with self.subTest(path=path, operation=operation.__name__), self.assertRaises(ValueError):
                    operation(path)

    def test_dot_files_and_directories_are_not_listed(self):
        (self.workspace / ".env").write_text("offline-private-value", encoding="utf-8")
        (self.workspace / ".git").mkdir()
        (self.workspace / "notes.txt").write_text("公开资料", encoding="utf-8")
        self.assertEqual(tool_call.list_files(), ["demo/", "notes.txt"])

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
                tool_call.read_file("external.py")
            with self.assertRaises(ValueError):
                tool_call.list_files("external.py")
            self.assertEqual(tool_call.list_files(), ["demo/"])

    def test_agent_browses_subdirectories_then_reads_sources(self):
        replies = [
            self.response([("call_project", "list_files", {"path": "demo"})]),
            self.response([("call_src", "list_files", {"path": "demo/src"}), ("call_main", "read_file", {"path": "demo/main.py"})]),
            self.response([("call_stats", "read_file", {"path": "demo/src/stats.py"})]),
            self.final,
        ]
        with patch("tool_call.call_model", side_effect=replies) as model:
            with patch("tool_call.write_file") as writer, patch("tool_call.run_python") as runner:
                with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                    reason = tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
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
                with patch("tool_call.call_model", return_value=self.response([("call_invalid", name, arguments)])):
                    with patch("tool_call.read_file") as reader, patch("tool_call.list_files") as lister:
                        with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                            tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
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
                with patch("tool_call.call_model", side_effect=replies) as model:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                        reason = tool_call.run_agent(copy.deepcopy(self.payload), "offline-placeholder", auto_read=True)
                approval.assert_not_called()
                self.assertEqual(reason, "stop")
                result = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
                self.assertFalse(result["ok"])
                self.assertEqual(result["path"], path)
                self.assertEqual(result["error"]["type"], error_type)


if __name__ == "__main__":
    unittest.main()
