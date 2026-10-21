import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call
from learn_agent import checkpoints, commands, permissions, tools


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.project = self.root / "检查点 项目"
        self.project.mkdir()
        self.target = self.project / "main.py"
        self.original = "# 用户已有备注\r\nvalues = [5, 10, 15]\r\nprint(sum(values))\r\n".encode("utf-8")
        self.target.write_bytes(self.original)
        self.unrelated = self.project / "user.txt"
        self.unrelated.write_bytes(b"user-owned untracked content\n")
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.log = self.logs / "history.jsonl"
        self.log.write_text('{"event":"run_end","reason":"stop"}\n', encoding="utf-8")
        workspace = patch.object(permissions, "WORKSPACE", self.project)
        workspace.start()
        self.addCleanup(workspace.stop)
        self.final = {"choices": [{"finish_reason": "stop", "message": {"content": "已根据结果完成。"}}]}

    def edit(self, old="[5, 10, 15]", new="[6, 12, 18]"):
        original, updated, _ = tools.prepare_edit("main.py", old, new)
        tools.apply_edit("main.py", original, updated)

    def tracked_edit(self):
        with checkpoints.track_changes(self.log):
            self.edit()
        return checkpoints.load_checkpoint(str(self.log))

    def run_cli(self, arguments, replies=(), answers=()):
        with patch.object(sys, "argv", ["tool_call.py", "--workspace", str(self.project), *arguments]):
            with patch.object(tool_call, "LOG_DIR", self.logs), patch("tool_call.get_api_key", return_value="offline-private-key") as key:
                with patch("learn_agent.providers.call_model", side_effect=replies) as model:
                    with patch("builtins.input", side_effect=answers) as approval:
                        with contextlib.redirect_stdout(io.StringIO()) as output:
                            tool_call.main()
        return key, model, approval, output.getvalue()

    def response(self, calls):
        return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for call_id, name, arguments in calls
        ]}}]}

    def test_edit_diff_and_undo_preserve_exact_existing_bytes(self):
        checkpoint = self.tracked_edit()
        self.assertTrue(checkpoint.data["finished"])
        self.assertEqual(checkpoints.decode(checkpoint.data["files"]["main.py"]["before"]), self.original)
        report = checkpoint.inspect()
        self.assertEqual(report["conflicts"], [])
        self.assertIn("-values = [5, 10, 15]", report["changes"][0]["diff"])
        self.assertIn("+values = [6, 12, 18]", report["changes"][0]["diff"])
        self.assertEqual(checkpoint.undo(), {"ok": True, "restored": ["main.py"], "removed": []})
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(self.unrelated.read_bytes(), b"user-owned untracked content\n")

    def test_multiple_writes_keep_first_before_and_last_after(self):
        with checkpoints.track_changes(self.log):
            tools.write_file("main.py", "value = 1\n")
            self.edit("value = 1", "value = 2")
            tools.write_file("main.py", "value = 3\n")
        checkpoint = checkpoints.load_checkpoint(str(self.log))
        entry = checkpoint.data["files"]["main.py"]
        self.assertEqual(checkpoints.decode(entry["before"]), self.original)
        self.assertEqual(checkpoints.decode(entry["after"]), self.target.read_bytes())
        checkpoint.undo()
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_undo_removes_only_new_files_recorded_by_file_tools(self):
        with checkpoints.track_changes(self.log):
            self.edit()
            tools.write_file("new.md", "本次报告\n")
            tools.write_file("empty.txt", "temporary")
            original, updated, _ = tools.prepare_edit("empty.txt", "temporary", "")
            tools.apply_edit("empty.txt", original, updated)
        checkpoint = checkpoints.load_checkpoint(str(self.log))
        self.assertEqual(checkpoint.inspect(reverse=True)["changes"][-1]["kind"], "删除")
        result = checkpoint.undo()
        self.assertEqual(result["removed"], ["new.md", "empty.txt"])
        self.assertFalse((self.project / "new.md").exists())
        self.assertFalse((self.project / "empty.txt").exists())
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertTrue(self.project.is_dir())
        self.assertTrue(self.unrelated.is_file())

    def test_binary_previous_contents_can_be_restored(self):
        binary = b"\x00\xff\x81binary"
        self.target.write_bytes(binary)
        with checkpoints.track_changes(self.log):
            tools.write_file("main.py", "replacement\n")
        checkpoint = checkpoints.load_checkpoint(str(self.log))
        self.assertIn("二进制", checkpoint.inspect()["changes"][0]["diff"])
        checkpoint.undo()
        self.assertEqual(self.target.read_bytes(), binary)

    def test_unchanged_write_has_no_content_diff(self):
        with checkpoints.track_changes(self.log):
            tools.write_file("main.py", tools.read_file("main.py"))
        checkpoint = checkpoints.load_checkpoint(str(self.log))
        self.assertEqual(checkpoint.inspect()["changes"], [])
        self.assertEqual(checkpoint.undo()["restored"], [])
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_later_user_change_blocks_entire_undo(self):
        with checkpoints.track_changes(self.log):
            self.edit()
            tools.write_file("new.md", "new file")
        edited = self.target.read_bytes() + b"# later user change\r\n"
        self.target.write_bytes(edited)
        checkpoint = checkpoints.load_checkpoint(str(self.log))
        checkpoint_bytes = checkpoint.path.read_bytes()
        self.assertEqual(checkpoint.inspect()["conflicts"], ["main.py"])
        with self.assertRaises(checkpoints.CheckpointConflict):
            checkpoint.undo()
        self.assertEqual(self.target.read_bytes(), edited)
        self.assertEqual((self.project / "new.md").read_text(encoding="utf-8"), "new file")
        self.assertEqual(checkpoint.path.read_bytes(), checkpoint_bytes)

    def test_identical_content_resaved_by_user_is_a_conflict(self):
        checkpoint = self.tracked_edit()
        info = self.target.stat()
        os.utime(self.target, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        self.assertEqual(checkpoint.inspect()["conflicts"], ["main.py"])
        with self.assertRaises(checkpoints.CheckpointConflict):
            checkpoint.undo()

    def test_later_changes_also_block_more_file_tool_writes(self):
        with checkpoints.track_changes(self.log):
            self.edit()
            external = self.target.read_bytes() + b"# external\n"
            self.target.write_bytes(external)
            with self.assertRaises(checkpoints.CheckpointConflict):
                tools.write_file("main.py", "overwrite external changes")
        self.assertEqual(self.target.read_bytes(), external)

    def test_deletion_or_directory_replacement_is_not_overwritten(self):
        checkpoint = self.tracked_edit()
        self.target.unlink()
        with self.assertRaises(checkpoints.CheckpointConflict):
            checkpoint.undo()
        self.target.mkdir()
        with self.assertRaises(checkpoints.CheckpointConflict):
            checkpoint.undo()
        self.assertTrue(self.target.is_dir())

    def test_outside_path_and_redirected_target_are_rejected(self):
        checkpoint = self.tracked_edit()
        outside = self.root / "outside.py"
        outside.write_bytes(b"outside original")
        resolve = Path.resolve

        def redirected(path, *args, **kwargs):
            return outside if path == self.target else resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", autospec=True, side_effect=redirected), self.assertRaises((ValueError, OSError)):
            checkpoint.undo()
        self.assertEqual(outside.read_bytes(), b"outside original")

    def test_unfinished_run_cannot_be_undone(self):
        with checkpoints.track_changes(self.log):
            self.edit()
            checkpoint = checkpoints.load_checkpoint(str(self.log))
            self.assertFalse(checkpoint.inspect()["finished"])
            with self.assertRaises(checkpoints.CheckpointConflict):
                checkpoint.undo()
        self.assertNotEqual(self.target.read_bytes(), self.original)

    def test_context_finishes_and_restores_active_checkpoint_on_interrupt(self):
        previous = checkpoints._ACTIVE
        with self.assertRaises(KeyboardInterrupt):
            with checkpoints.track_changes(self.log):
                self.edit()
                raise KeyboardInterrupt()
        self.assertIs(checkpoints._ACTIVE, previous)
        checkpoint = checkpoints.load_checkpoint(str(self.log))
        self.assertTrue(checkpoint.data["finished"])
        checkpoint.undo()
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_checkpoint_storage_failure_happens_before_file_write(self):
        with checkpoints.track_changes(self.log) as checkpoint:
            with patch.object(checkpoint, "save", side_effect=OSError("disk full")), self.assertRaises(OSError):
                tools.write_file("main.py", "changed")
            self.assertEqual(self.target.read_bytes(), self.original)
            self.assertEqual(checkpoint.data["files"], {})

    def test_checkpoint_save_failure_around_run_records_error_end(self):
        save = checkpoints.Checkpoint.save
        for failed_call in (1, 2):
            with self.subTest(failed_call=failed_call):
                calls = 0

                def save_checkpoint(checkpoint):
                    nonlocal calls
                    calls += 1
                    if calls == failed_call:
                        raise OSError("checkpoint storage unavailable")
                    return save(checkpoint)

                previous = checkpoints._ACTIVE
                with patch.object(checkpoints.Checkpoint, "save", autospec=True, side_effect=save_checkpoint):
                    with self.assertRaises(SystemExit):
                        self.run_cli(["只回答"], [self.final])
                log = sorted(self.logs.glob("run-*.jsonl"))[-1]
                entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
                self.assertEqual((entries[-1]["event"], entries[-1]["reason"]), ("run_end", "error"))
                self.assertEqual(entries[-1]["error_type"], "OSError")
                self.assertIs(checkpoints._ACTIVE, previous)
                self.assertEqual(self.target.read_bytes(), self.original)

    def test_partial_write_error_keeps_snapshot_and_refuses_automatic_undo(self):
        write_bytes = Path.write_bytes

        def fail_write(path, content):
            if path == self.target:
                write_bytes(path, b"partial")
                raise OSError("partial write")
            return write_bytes(path, content)

        with checkpoints.track_changes(self.log):
            with patch.object(Path, "write_bytes", autospec=True, side_effect=fail_write), self.assertRaises(OSError):
                self.edit()
        checkpoint = checkpoints.load_checkpoint(str(self.log))
        self.assertEqual(checkpoints.decode(checkpoint.data["files"]["main.py"]["before"]), self.original)
        self.assertEqual(checkpoint.inspect()["conflicts"], ["main.py"])
        with self.assertRaises(checkpoints.CheckpointConflict):
            checkpoint.undo()
        self.assertEqual(self.target.read_bytes(), b"partial")

    def test_partial_undo_is_recorded_and_never_claimed_complete(self):
        with checkpoints.track_changes(self.log):
            self.edit()
            tools.write_file("new.md", "new")
        checkpoint = checkpoints.load_checkpoint(str(self.log))
        unlink = Path.unlink

        def fail_unlink(path, *args, **kwargs):
            if path == self.project / "new.md":
                raise PermissionError("denied")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", autospec=True, side_effect=fail_unlink), self.assertRaises(checkpoints.CheckpointConflict):
            checkpoint.undo()
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertTrue((self.project / "new.md").exists())
        reloaded = checkpoints.load_checkpoint(str(self.log))
        self.assertEqual(reloaded.data["files"]["main.py"]["state"], "undone")
        self.assertEqual(reloaded.data["files"]["new.md"]["state"], "undo_pending")
        with self.assertRaises(checkpoints.CheckpointConflict):
            reloaded.undo()

    def test_current_log_and_checkpoint_are_not_file_tool_targets(self):
        with patch.object(permissions, "WORKSPACE", self.root):
            with checkpoints.track_changes(self.log) as checkpoint:
                for target in (self.log, checkpoint.path):
                    with self.subTest(target=target), self.assertRaises(checkpoints.CheckpointConflict):
                        tools.write_file(target.relative_to(self.root).as_posix(), "corrupt trace")
                self.assertEqual(checkpoint.data["files"], {})

    def test_command_changes_are_not_claimed_by_file_checkpoint(self):
        with checkpoints.track_changes(self.log):
            self.edit()
            result = commands.run_command(["python", "-c", "from pathlib import Path; Path('generated.txt').write_text('command output'); Path('main.py').write_text('command changed source')"])
            self.assertTrue(result["ok"])
        checkpoint = checkpoints.load_checkpoint(str(self.log))
        self.assertEqual(list(checkpoint.data["files"]), ["main.py"])
        with self.assertRaises(checkpoints.CheckpointConflict):
            checkpoint.undo()
        self.assertEqual(self.target.read_text(encoding="utf-8"), "command changed source")
        self.assertEqual((self.project / "generated.txt").read_text(), "command output")

    def test_git_staged_and_unstaged_changes_survive_undo(self):
        def git(*arguments):
            return subprocess.check_output(["git", "--no-optional-locks", "-C", str(self.project), *arguments], stderr=subprocess.DEVNULL)

        git("init")
        git("config", "core.autocrlf", "false")
        self.target.write_bytes(b"values = [1, 2, 3]\n")
        git("add", "main.py")
        git("-c", "user.name=checkpoint-test", "-c", "user.email=checkpoint-test@example.invalid", "commit", "-m", "baseline")
        self.target.write_bytes(b"values = [4, 8, 12]\n")
        git("add", "main.py")
        self.target.write_bytes(self.original)
        index = git("show", ":main.py")
        staged = git("diff", "--cached", "--binary")
        status = git("status", "--porcelain", "-z")
        index_bytes = (self.project / ".git/index").read_bytes()
        checkpoint = self.tracked_edit()
        self.assertIn("MM main.py", checkpoint.data["git_before"])
        checkpoint.undo()
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(git("show", ":main.py"), index)
        self.assertEqual(git("diff", "--cached", "--binary"), staged)
        self.assertEqual((self.project / ".git/index").read_bytes(), index_bytes)
        self.assertEqual(git("status", "--porcelain", "-z"), status)

    def test_wrong_project_and_malformed_checkpoint_are_rejected(self):
        checkpoint = self.tracked_edit()
        with patch.object(permissions, "WORKSPACE", self.root), self.assertRaises(ValueError):
            checkpoints.load_checkpoint(str(self.log))
        original = copy.deepcopy(checkpoint.data)
        cases = [[], {**original, "version": 99}, {**original, "version": True}, {**original, "finished": "yes"}]
        for path in ("../outside.py", ".env", "./main.py", str(self.target)):
            changed = copy.deepcopy(original)
            changed["files"] = {path: changed["files"]["main.py"]}
            cases.append(changed)
        for field, value in (("after", "invalid base64!"), ("after", None), ("before", 123), ("state", "invalid"), ("fingerprint", {})):
            changed = copy.deepcopy(original)
            changed["files"]["main.py"][field] = value
            cases.append(changed)
        for data in cases:
            with self.subTest(data=data):
                checkpoint.path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises((ValueError, OSError)):
                    checkpoints.load_checkpoint(str(self.log))
        checkpoint.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            checkpoints.load_checkpoint(str(self.log))

    def test_cli_diff_cancel_and_confirm_undo_do_not_call_model_or_change_log(self):
        reply = self.response([("edit", "edit_file", {"path": "main.py", "old_text": "[5, 10, 15]", "new_text": "[6, 12, 18]"})])
        running = self.response([("run", "run_command", {"command": ["python", "main.py"]})])
        self.run_cli(["--auto-read", "修改并验证"], [reply, running, self.final], ["y", "y"])
        log = next(self.logs.glob("run-*.jsonl"))
        original_log = log.read_bytes()
        entries = [json.loads(line) for line in original_log.decode("utf-8").splitlines()]
        self.assertEqual(entries[0]["checkpoint"], str(log.with_suffix(".checkpoint.json")))
        result = next(entry["result"] for entry in entries if entry["event"] == "tool_result" and entry["tool"] == "run_command")
        self.assertEqual(result["stdout"], "36\n")
        changed = self.target.read_bytes()
        for arguments, answers in [(["--diff", str(log)], []), (["--undo", str(log)], ["n"])]:
            key, model, _, output = self.run_cli(arguments, answers=answers)
            key.assert_not_called()
            model.assert_not_called()
            self.assertIn("values = [6, 12, 18]", output)
            self.assertEqual(self.target.read_bytes(), changed)
        key, model, approval, output = self.run_cli(["--undo", str(log)], answers=["y"])
        key.assert_not_called()
        model.assert_not_called()
        approval.assert_called_once()
        self.assertIn('"ok": true', output)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(log.read_bytes(), original_log)
        self.assertNotIn("offline-private-key", log.with_suffix(".checkpoint.json").read_text(encoding="utf-8"))
        self.target.write_bytes(b"user changed after undo\n")
        _, _, approval, _ = self.run_cli(["--undo", str(log)])
        approval.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"user changed after undo\n")

    def test_fixed_read_only_file_cannot_be_restored_from_forged_entry(self):
        checkpoint = self.tracked_edit()
        protected = self.project / "test_main.py"
        protected.write_bytes(b"fixed acceptance test")
        checkpoint.data["files"] = {"test_main.py": checkpoint.data["files"]["main.py"]}
        checkpoint.save()
        with patch.object(permissions, "DEFAULT_WORKSPACE", self.project), self.assertRaises(ValueError):
            checkpoints.load_checkpoint(str(self.log))
        self.assertEqual(protected.read_bytes(), b"fixed acceptance test")

    def test_cli_rechecks_changes_after_undo_confirmation(self):
        self.tracked_edit()
        external = b"later user content\n"

        def approve(_):
            self.target.write_bytes(external)
            return "y"

        with patch.object(sys, "argv", ["tool_call.py", "--workspace", str(self.project), "--undo", str(self.log)]):
            with patch("tool_call.get_api_key") as key, patch("learn_agent.providers.call_model") as model:
                with patch("builtins.input", side_effect=approve), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    tool_call.main()
        key.assert_not_called()
        model.assert_not_called()
        self.assertEqual(self.target.read_bytes(), external)

    def test_denied_file_tool_does_not_create_owned_changes(self):
        reply = self.response([("edit", "edit_file", {"path": "main.py", "old_text": "[5, 10, 15]", "new_text": "[6, 12, 18]"})])
        self.run_cli(["修改"], [reply], ["n"])
        log = next(self.logs.glob("run-*.jsonl"))
        self.assertEqual(checkpoints.load_checkpoint(str(log)).data["files"], {})
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_review_flags_do_not_mix_with_preview_resume_or_new_task(self):
        for arguments in (["--diff", str(self.log), "--preview"], ["--diff", str(self.log), "new task"],
                          ["--undo", str(self.log), "--resume", str(self.log)]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_cli(arguments)
        self.assertEqual(self.target.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
