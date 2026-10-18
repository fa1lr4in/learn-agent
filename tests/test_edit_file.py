import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call


class EditFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="learn-agent-test-")
        self.root = Path(self.directory.name)
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.directory.cleanup)
        self.workspace = self.root / "workspace"
        (self.workspace / "demo").mkdir(parents=True)
        workspace_patch = patch.object(tool_call, "WORKSPACE", self.workspace)
        workspace_patch.start()
        self.addCleanup(workspace_patch.stop)
        self.path = "demo/main.py"
        self.target = self.workspace / self.path
        self.code = "from helpers import total\n\nvalues = [12, 18, 30]\nprint(total(values))\n"
        self.target.write_bytes(self.code.encode("utf-8"))
        self.helper = self.workspace / "demo/helpers.py"
        self.helper.write_bytes(b"def total(values):\n    return sum(values)\n")
        self.arguments = {"path": self.path, "old_text": "values = [12, 18, 30]", "new_text": "values = [5, 10, 15]"}
        self.payload = {"messages": [{"role": "system", "content": "test"}, {"role": "user", "content": "局部修改"}]}
        self.final = {"choices": [{"finish_reason": "stop", "message": {"content": "已根据真实结果回答。"}}]}

    def response(self, calls):
        return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for call_id, name, arguments in calls
        ]}}]}

    def test_preview_does_not_modify_and_commit_changes_only_one_fragment(self):
        before = self.target.read_bytes()
        original, updated, diff = tool_call.prepare_edit(**self.arguments)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(original, before)
        self.assertEqual(updated, self.code.replace("[12, 18, 30]", "[5, 10, 15]").encode("utf-8"))
        self.assertIn("--- a/demo/main.py", diff)
        self.assertIn("+++ b/demo/main.py", diff)
        self.assertIn("-values = [12, 18, 30]", diff)
        self.assertIn("+values = [5, 10, 15]", diff)
        tool_call.apply_edit(self.path, original, updated)
        self.assertEqual(self.target.read_bytes(), updated)

    def test_read_edit_run_and_final_log_form_one_complete_task(self):
        replies = [
            self.response([("read", "read_file", {"path": self.path})]),
            self.response([("edit", "edit_file", self.arguments)]),
            self.response([("run", "run_python", {"path": self.path})]),
            self.final,
        ]
        log_dir = self.root / "logs"
        helper_before = self.helper.read_bytes()
        with patch.object(tool_call, "LOG_DIR", log_dir), patch.object(sys, "argv", ["tool_call.py", "--auto-read", "局部修改"]):
            with patch("tool_call.get_api_key", return_value="offline-placeholder"), patch("tool_call.call_model", side_effect=replies):
                with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                    tool_call.main()
        self.assertEqual(approval.call_count, 2)
        entries = [json.loads(line) for line in next(log_dir.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        results = [entry["result"] for entry in entries if entry["event"] == "tool_result"]
        self.assertEqual([result["tool"] for result in results], ["read_file", "edit_file", "run_python"])
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(results[1]["replacements"], 1)
        self.assertIn("+values = [5, 10, 15]", results[1]["diff"])
        self.assertEqual(results[2]["exit_code"], 0)
        self.assertEqual(results[2]["stdout"].strip(), "30")
        self.assertEqual(results[2]["stderr"], "")
        self.assertEqual(entries[-1]["event"], "run_end")
        self.assertEqual(entries[-1]["reason"], "stop")
        self.assertEqual(self.helper.read_bytes(), helper_before)
        decisions = [entry["source"] for entry in entries if entry["event"] == "tool_approval"]
        self.assertEqual(decisions, ["auto_read", "user", "user"])

    def test_missing_ambiguous_overlapping_and_unchanged_text_do_not_write(self):
        for source, old, new in [("one\n", "missing", "new"), ("one one\n", "one", "new"),
                                 ("aaa", "aa", "new"), ("one\n", "one", "one"),
                                 ("one\r\none\n", "one\n", "new\n")]:
            with self.subTest(source=source, old=old):
                original = source.encode("utf-8")
                self.target.write_bytes(original)
                reply = self.response([("edit", "edit_file", {"path": self.path, "old_text": old, "new_text": new})])
                with patch("tool_call.call_model", side_effect=[reply, self.final]) as model:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                        reason = tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
                self.assertEqual(reason, "stop")
                approval.assert_not_called()
                self.assertEqual(self.target.read_bytes(), original)
                result = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["type"], "EditConflict")

    def test_model_receives_conflict_and_can_retry_with_better_context(self):
        wrong = {**self.arguments, "old_text": "old code that is not present"}
        replies = [self.response([("wrong", "edit_file", wrong)]),
                   self.response([("correct", "edit_file", self.arguments)]), self.final]
        with patch("tool_call.call_model", side_effect=replies) as model:
            with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                reason = tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
        self.assertEqual(reason, "stop")
        approval.assert_called_once()
        conflict = json.loads(model.call_args_list[1].args[0]["messages"][-1]["content"])
        self.assertEqual(conflict["error"]["type"], "EditConflict")
        self.assertIn("未匹配", conflict["error"]["message"])
        self.assertIn(b"[5, 10, 15]", self.target.read_bytes())

    def test_refusal_sees_diff_first_and_preserves_file_and_remaining_tools(self):
        reply = self.response([("edit", "edit_file", self.arguments), ("run", "run_python", {"path": self.path})])
        original = self.target.read_bytes()
        log_file, output = io.StringIO(), io.StringIO()

        def reject(prompt):
            self.assertIn("-values = [12, 18, 30]", output.getvalue())
            self.assertIn("+values = [5, 10, 15]", output.getvalue())
            self.assertIn("按差异编辑 demo/main.py", prompt)
            return "n"

        with patch("tool_call.call_model", return_value=reply) as model, patch("tool_call.run_python") as runner:
            with patch("builtins.input", side_effect=reject), contextlib.redirect_stdout(output):
                reason = tool_call.run_agent(self.payload, "offline-placeholder", log_file, auto_read=True)
        self.assertEqual(reason, "user_denied")
        model.assert_called_once()
        runner.assert_not_called()
        self.assertEqual(self.target.read_bytes(), original)
        entries = [json.loads(line) for line in log_file.getvalue().splitlines()]
        self.assertFalse(any(entry["event"] == "tool_result" for entry in entries))
        self.assertFalse(entries[-1]["approved"])

    def test_file_changed_during_approval_is_not_overwritten(self):
        external = b"# changed by the user while inspecting the diff\n" + self.target.read_bytes()

        def approve_after_external_change(prompt):
            self.target.write_bytes(external)
            return "y"

        with patch("tool_call.call_model", side_effect=[self.response([("edit", "edit_file", self.arguments)]), self.final]) as model:
            with patch("builtins.input", side_effect=approve_after_external_change), contextlib.redirect_stdout(io.StringIO()):
                reason = tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
        self.assertEqual(reason, "stop")
        self.assertEqual(self.target.read_bytes(), external)
        result = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
        self.assertFalse(result["ok"])
        self.assertIn("预览后文件已变化", result["error"]["message"])

    def test_line_endings_and_unrelated_utf8_bytes_are_preserved(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                before = ("\ufeff# 中文注释" + newline + "left = 1" + newline + "right = 2" + newline + "# 尾部").encode("utf-8")
                self.target.write_bytes(before)
                original, updated, _ = tool_call.prepare_edit(self.path, "left = 1\nright = 2", "left = 3\nright = 4")
                expected = before.replace(("left = 1" + newline + "right = 2").encode(),
                                          ("left = 3" + newline + "right = 4").encode())
                tool_call.apply_edit(self.path, original, updated)
                self.assertEqual(self.target.read_bytes(), expected)
        before = "前言\r\nleft = 1\nright = 2\r\n尾部\r\n".encode("utf-8")
        self.target.write_bytes(before)
        original, updated, _ = tool_call.prepare_edit(self.path, "left = 1\nright = 2", "left = 3\nright = 4")
        tool_call.apply_edit(self.path, original, updated)
        self.assertEqual(self.target.read_bytes(), before.replace(b"left = 1\nright = 2", b"left = 3\nright = 4"))

    def test_matching_is_literal_and_deletion_can_leave_an_empty_file(self):
        self.target.write_bytes(b"a[0] = 1\n")
        original, updated, _ = tool_call.prepare_edit(self.path, "a[0] = 1", "a[0] = 2")
        tool_call.apply_edit(self.path, original, updated)
        self.assertEqual(self.target.read_bytes(), b"a[0] = 2\n")
        original, updated, diff = tool_call.prepare_edit(self.path, "a[0] = 2\n", "")
        tool_call.apply_edit(self.path, original, updated)
        self.assertTrue(self.target.is_file())
        self.assertEqual(self.target.read_bytes(), b"")
        self.assertIn("-a[0] = 2", diff)

    def test_diff_marks_a_missing_final_newline(self):
        self.target.write_bytes(b"old")
        _, updated, diff = tool_call.prepare_edit(self.path, "old", "new")
        self.assertEqual(updated, b"new")
        self.assertIn("-old\n\\ No newline at end of file\n", diff)
        self.assertIn("+new\n\\ No newline at end of file\n", diff)

    def test_invalid_arguments_and_read_only_or_outside_paths_stop_before_preparing(self):
        invalid = [
            {**self.arguments, "old_text": ""}, {**self.arguments, "old_text": None},
            {**self.arguments, "new_text": None}, {**self.arguments, "extra": True},
            {"path": self.path, "old_text": "values"},
            *[{**self.arguments, "path": path} for path in ("../outside.py", ".env", "demo/.git/config", *tool_call.READ_ONLY_FILES)],
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with patch("tool_call.call_model", return_value=self.response([("bad", "edit_file", arguments)])):
                    with patch("tool_call.prepare_edit") as prepare, patch("builtins.input") as approval:
                        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                            tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
                prepare.assert_not_called()
                approval.assert_not_called()

    def test_missing_file_and_invalid_utf8_are_reported_without_approval(self):
        (self.workspace / "demo/binary.txt").write_bytes(b"\xff")
        for path, error_type in [("demo/missing.py", "FileNotFoundError"), ("demo/binary.txt", "UnicodeDecodeError")]:
            with self.subTest(path=path):
                reply = self.response([("error", "edit_file", {**self.arguments, "path": path})])
                with patch("tool_call.call_model", side_effect=[reply, self.final]) as model:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                        tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
                approval.assert_not_called()
                result = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
                self.assertEqual(result["path"], path)
                self.assertEqual(result["error"]["type"], error_type)

    def test_same_batch_prepares_each_edit_after_the_previous_write(self):
        second = {"path": self.path, "old_text": "print(total(values))", "new_text": "print('total:', total(values))"}
        reply = self.response([("first", "edit_file", self.arguments), ("second", "edit_file", second)])
        with patch("tool_call.call_model", side_effect=[reply, self.final]) as model:
            with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                reason = tool_call.run_agent(self.payload, "offline-placeholder", auto_read=True)
        self.assertEqual(reason, "stop")
        self.assertEqual(approval.call_count, 2)
        results = [json.loads(message["content"]) for message in model.call_args_list[-1].args[0]["messages"] if message["role"] == "tool"]
        self.assertTrue(all(result["ok"] for result in results))
        self.assertIn(b"[5, 10, 15]", self.target.read_bytes())
        self.assertIn(b"print('total:', total(values))", self.target.read_bytes())

    def test_preview_advertises_edit_without_reading_writing_or_network(self):
        with patch.object(sys, "argv", ["tool_call.py", "--preview"]), patch("tool_call.get_api_key") as key:
            with patch("tool_call.call_model") as model, patch("tool_call.prepare_edit") as prepare, patch("tool_call.apply_edit") as apply:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    tool_call.main()
        payload, _ = json.JSONDecoder().raw_decode(output.getvalue().split("\n", 1)[1])
        schema = next(item["function"] for item in payload["tools"] if item["function"]["name"] == "edit_file")
        self.assertEqual(schema["parameters"]["required"], ["path", "old_text", "new_text"])
        for operation in (key, model, prepare, apply):
            operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
