import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call


class SearchTextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="learn-agent-test-")
        self.root = Path(self.directory.name)
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.directory.cleanup)
        self.workspace = self.root / "workspace"
        workspace_patch = patch.object(tool_call, "WORKSPACE", self.workspace)
        workspace_patch.start()
        self.addCleanup(workspace_patch.stop)
        self.sources = {
            "demo/main.py": "from src.stats import summarize\nvalues = [5, 10, 15]\nprint(summarize(values))\n",
            "demo/src/stats.py": "def summarize(values):\n    return sum(values)\n",
            "demo/notes.txt": "中文统计\nliteral [a.*]\nSUMMARIZE\n",
        }
        for name, text in self.sources.items():
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        self.payload = {"messages": [{"role": "system", "content": "test"}, {"role": "user", "content": "搜索定位"}]}
        self.final = {"choices": [{"finish_reason": "stop", "message": {"content": "已根据搜索与源码回答。"}}]}

    def response(self, calls):
        return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for call_id, name, arguments in calls
        ]}}]}

    def test_returns_workspace_paths_and_one_based_line_numbers(self):
        result = tool_call.search_text("summarize")
        self.assertEqual([(item["path"], item["line"]) for item in result["matches"]],
                         [("demo/main.py", 1), ("demo/main.py", 3), ("demo/src/stats.py", 1)])
        self.assertEqual(result["scope"], "visible_workspace_text")
        self.assertEqual(result["path"], ".")
        self.assertEqual(result["query"], "summarize")
        self.assertFalse(result["truncated"])
        self.assertTrue(all(not item["text_truncated"] for item in result["matches"]))
        self.assertEqual(result["skipped_files"], 0)
        self.assertEqual(result["skipped_directories"], 0)

    def test_chinese_and_regex_metacharacters_are_literal_and_case_sensitive(self):
        for query, number in [("中文", 1), ("[a.*]", 2), ("SUMMARIZE", 3)]:
            with self.subTest(query=query):
                result = tool_call.search_text(query, "demo")
                self.assertEqual([(item["path"], item["line"]) for item in result["matches"]], [("demo/notes.txt", number)])
        self.assertEqual(tool_call.search_text("Summarize", "demo")["matches"], [])

    def test_result_limit_is_only_truncated_when_more_matches_exist(self):
        limited = tool_call.search_text("summarize", "demo", 1)
        self.assertEqual(len(limited["matches"]), 1)
        self.assertTrue(limited["truncated"])
        exact = tool_call.search_text("summarize", "demo", 3)
        self.assertEqual(len(exact["matches"]), 3)
        self.assertFalse(exact["truncated"])
        narrowed = tool_call.search_text("summarize", "demo/src", 1)
        self.assertEqual(len(narrowed["matches"]), 1)
        self.assertFalse(narrowed["truncated"])

    def test_long_line_excerpt_contains_the_match_and_is_bounded(self):
        (self.workspace / "demo/long.txt").write_bytes(("x" * 300 + "关键词" + "y" * 400).encode("utf-8"))
        item = tool_call.search_text("关键词", "demo")["matches"][0]
        self.assertEqual(item["line"], 1)
        self.assertEqual(len(item["text"]), 200)
        self.assertIn("关键词", item["text"])
        self.assertTrue(item["text_truncated"])

    def test_excludes_hidden_dependencies_binary_invalid_and_large_files(self):
        for directory in (*tool_call.SEARCH_IGNORED_DIRS, ".git", "BUILD"):
            target = self.workspace / "demo" / directory / "ignored.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"needle\n")
        (self.workspace / ".env").write_bytes(b"needle\n")
        (self.workspace / "demo/public.txt").write_bytes(b"needle\n")
        (self.workspace / "demo/binary.bin").write_bytes(b"needle\x00")
        (self.workspace / "demo/invalid.txt").write_bytes(b"needle\xff")
        (self.workspace / "demo/large.txt").write_bytes(b"needle" + b"x" * tool_call.MAX_SEARCH_FILE_BYTES)
        result = tool_call.search_text("needle")
        self.assertEqual([item["path"] for item in result["matches"]], ["demo/public.txt"])
        self.assertEqual(result["skipped_files"], 3)
        self.assertFalse(result["truncated"])

    def test_resolved_links_are_skipped_even_when_target_is_inside_workspace(self):
        alias = self.workspace / "demo/alias.py"
        internal = self.workspace / "demo/internal.py"
        loop = self.workspace / "demo/loop"
        loop.mkdir()
        for file in (alias, internal, loop / "hidden.txt"):
            file.write_bytes(b"needle\n")
        outside = self.root / "outside.py"
        outside.write_bytes(b"needle\n")
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            targets = {alias: outside, internal: self.workspace / "demo/src/stats.py", loop: self.workspace / "demo"}
            return targets.get(path, original_resolve(path, *args, **kwargs))

        with patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            self.assertEqual(tool_call.search_text("needle")["matches"], [])

    def test_unreadable_files_and_directories_report_partial_search_counts(self):
        original_open, original_walk = Path.open, os.walk

        def open_file(path, *args, **kwargs):
            if path.name == "notes.txt":
                raise PermissionError("private permission detail")
            return original_open(path, *args, **kwargs)

        def walk(*args, **kwargs):
            kwargs["onerror"](PermissionError("private directory detail"))
            yield from original_walk(*args, **kwargs)

        with patch.object(Path, "open", autospec=True, side_effect=open_file), patch("tool_call.os.walk", side_effect=walk):
            result = tool_call.search_text("summarize")
        self.assertEqual(len(result["matches"]), 3)
        self.assertEqual(result["skipped_files"], 1)
        self.assertEqual(result["skipped_directories"], 1)
        self.assertNotIn("private", json.dumps(result))

    def test_agent_narrows_search_reads_sources_and_records_stop_without_writes(self):
        replies = [
            self.response([("broad", "search_text", {"query": "summarize", "path": "demo", "max_results": 1})]),
            self.response([("narrow", "search_text", {"query": "summarize", "path": "demo/src"})]),
            self.response([("read_def", "read_file", {"path": "demo/src/stats.py"}),
                           ("read_entry", "read_file", {"path": "demo/main.py"})]),
            self.final,
        ]
        log_dir = self.root / "logs"
        before = {name: (self.workspace / name).read_bytes() for name in self.sources}
        with patch.object(tool_call, "LOG_DIR", log_dir), patch.object(sys, "argv", ["tool_call.py", "--auto-read", "搜索定位"]):
            with patch("tool_call.get_api_key", return_value="offline-placeholder"), patch("tool_call.call_model", side_effect=replies):
                with patch("tool_call.write_file") as writer, patch("tool_call.apply_edit") as editor, patch("tool_call.run_python") as runner:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                        tool_call.main()
        for operation in (writer, editor, runner, approval):
            operation.assert_not_called()
        entries = [json.loads(line) for line in next(log_dir.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        results = [entry["result"] for entry in entries if entry["event"] == "tool_result"]
        self.assertTrue(results[0]["truncated"])
        self.assertFalse(results[1]["truncated"])
        self.assertEqual(results[1]["matches"][0]["path"], "demo/src/stats.py")
        self.assertEqual(results[1]["matches"][0]["line"], 1)
        self.assertEqual(results[2]["content"], self.sources["demo/src/stats.py"])
        self.assertEqual(entries[-1]["reason"], "stop")
        self.assertTrue(all(entry["source"] == "auto_read" for entry in entries if entry["event"] == "tool_approval"))
        for name, content in before.items():
            self.assertEqual((self.workspace / name).read_bytes(), content)

    def test_default_mode_requires_search_approval_and_denial_does_not_search(self):
        reply = self.response([("search", "search_text", {"query": "summarize"})])
        for answer in ("y", "n"):
            with self.subTest(answer=answer):
                with patch("tool_call.call_model", side_effect=[reply, self.final]) as model:
                    with patch("tool_call.search_text", return_value={"matches": [], "truncated": False}) as search:
                        with patch("builtins.input", return_value=answer) as approval, contextlib.redirect_stdout(io.StringIO()):
                            reason = tool_call.run_agent(self.payload, "offline-placeholder")
                approval.assert_called_once()
                self.assertEqual(reason, "stop" if answer == "y" else "user_denied")
                self.assertEqual(model.call_count, 2 if answer == "y" else 1)
                if answer == "y":
                    search.assert_called_once_with(query="summarize")
                else:
                    search.assert_not_called()

    def test_invalid_parameters_and_paths_are_rejected_before_confirmation(self):
        invalid = [{}, {"query": None}, {"query": ""}, {"query": "  "}, {"query": "two\nlines"},
                   {"query": "word", "extra": True}, {"query": "word", "path": None},
                   *[{"query": "word", "max_results": value} for value in (0, 51, True, 1.5, "2", None)],
                   *[{"query": "word", "path": path} for path in ("..", ".git", str(self.root / "outside"))]]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                reply = self.response([("bad", "search_text", arguments)])
                with patch("tool_call.call_model", return_value=reply), patch("tool_call.search_text") as search:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                        tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
                search.assert_not_called()
                approval.assert_not_called()

    def test_empty_search_is_success_and_wrong_directory_is_a_tool_error(self):
        result = tool_call.search_text("not present", "demo")
        self.assertEqual(result["matches"], [])
        self.assertFalse(result["truncated"])
        for path in ("missing", "demo/main.py"):
            with self.subTest(path=path):
                reply = self.response([("error", "search_text", {"query": "word", "path": path})])
                with patch("tool_call.call_model", side_effect=[reply, self.final]) as model:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                        tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
                approval.assert_not_called()
                error = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
                self.assertFalse(error["ok"])
                self.assertEqual(error["path"], path)
                self.assertEqual(error["error"]["type"], "NotADirectoryError")

    def test_default_limit_returns_twenty_rows_and_maximum_can_return_more(self):
        (self.workspace / "demo/many.txt").write_bytes(b"needle\n" * 25)
        result = tool_call.search_text("needle", "demo")
        self.assertEqual(len(result["matches"]), 20)
        self.assertTrue(result["truncated"])
        result = tool_call.search_text("needle", "demo", 50)
        self.assertEqual(len(result["matches"]), 25)
        self.assertFalse(result["truncated"])

    def test_preview_advertises_search_without_key_network_or_file_search(self):
        with patch.object(sys, "argv", ["tool_call.py", "--preview"]), patch.object(tool_call, "LOG_DIR", self.root / "logs"):
            with patch("tool_call.get_api_key") as key, patch("tool_call.call_model") as model, patch("tool_call.search_text") as search:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    tool_call.main()
        payload, _ = json.JSONDecoder().raw_decode(output.getvalue().split("\n", 1)[1])
        schema = next(item["function"] for item in payload["tools"] if item["function"]["name"] == "search_text")
        self.assertEqual(schema["parameters"]["required"], ["query"])
        self.assertEqual(schema["parameters"]["properties"]["max_results"]["maximum"], 50)
        self.assertFalse((self.root / "logs").exists())
        for operation in (key, model, search):
            operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
