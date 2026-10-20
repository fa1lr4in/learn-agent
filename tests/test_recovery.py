import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import tool_call
from learn_agent import tools
from learn_agent.recovery import load_recovery
from learn_agent.sessions import SessionStore


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-recovery-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "中文 项目"
        self.project.mkdir()
        (self.project / "main.py").write_text("print(5050)\n", encoding="utf-8")
        (self.project / "notes.txt").write_text("实验编号：7391", encoding="utf-8")
        self.logs = self.root / "logs"
        patcher = patch.object(tool_call, "LOG_DIR", self.logs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.final = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "已核对。"}}]}

    def reply(self, *calls):
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
                for call_id, name, arguments in (calls or [("old_run", "run_python", {"path": "main.py"})])
            ],
        }}]}

    def run_cli(self, args, replies=(), answer="y", error=False):
        before = set(self.logs.glob("*.jsonl"))
        output = io.StringIO()
        argv = ["tool_call.py", "--workspace", str(self.project), *args]
        with patch.object(sys, "argv", argv), patch("tool_call.get_api_key", return_value="offline-key") as key:
            with patch("learn_agent.providers.call_model", side_effect=replies) as model:
                with patch("builtins.input", side_effect=answer if callable(answer) else None,
                           return_value=None if callable(answer) else answer) as approval:
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                        if error:
                            with self.assertRaises(SystemExit) as caught:
                                tool_call.main()
                            output.write(str(caught.exception))
                        else:
                            tool_call.main()
        paths = set(self.logs.glob("*.jsonl")) - before
        return next(iter(paths)) if paths else None, model, key, approval, output.getvalue()

    def entries(self, path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def write_log(self, entries, name="copy.jsonl"):
        path = self.root / name
        path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries), encoding="utf-8")
        return path

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def cancelled_source(self):
        def interrupt(prompt):
            raise KeyboardInterrupt()
        path, _, _, _, _ = self.run_cli(["运行脚本"], [self.reply()], interrupt, error=True)
        return path

    def test_cancel_before_execution_records_safe_pending_tool(self):
        with patch("learn_agent.tools.run_python") as runner:
            source = self.cancelled_source()
        runner.assert_not_called()
        events = self.entries(source)
        self.assertEqual((events[-1]["reason"], events[-1]["task_state"]), ("cancelled", "cancelled"))
        self.assertNotIn("tool_started", [entry["event"] for entry in events])
        plan = load_recovery(str(source), self.project)
        plan.require_safe()
        self.assertEqual(plan.tools[0]["status"], "not_executed")
        self.assertEqual(json.loads(plan.history[-1]["content"])["error"]["type"], "NotExecuted")

    def test_recovery_preview_has_no_api_tool_or_storage_side_effects(self):
        source = self.cancelled_source()
        before = self.snapshot()
        with patch("learn_agent.tools.run_python") as runner:
            path, model, key, approval, output = self.run_cli(["--recover", str(source), "--preview", "继续任务"])
        self.assertIsNone(path)
        for operation in (model, key, approval, runner):
            operation.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertIn("确定未执行", output)
        self.assertIn("NotExecuted", output)

    def test_recovery_runs_only_new_request_and_preserves_source_session(self):
        source = self.cancelled_source()
        source_bytes = source.read_bytes()
        store = SessionStore(self.logs / "sessions.sqlite3")
        original = store.list_sessions()[0]
        original_data = store.read(original.id)
        response = self.reply(("new_run", "run_python", {"path": "main.py"}))
        with patch("learn_agent.tools.run_python", wraps=tools.run_python) as runner:
            path, model, _, approval, _ = self.run_cli(["--recover", str(source), "重新请求运行"], [response, self.final])
        runner.assert_called_once_with("main.py")
        approval.assert_called_once()
        self.assertEqual(model.call_count, 2)
        events = self.entries(path)
        self.assertEqual(events[0]["recovered_from"], str(source.resolve()))
        self.assertIsNone(events[0]["resumed_from"])
        self.assertNotEqual(events[0]["session_id"], original.id)
        result = next(entry["result"] for entry in events if entry["event"] == "tool_result")
        self.assertEqual((result["ok"], result["exit_code"], result["stdout"]), (True, 0, "5050\n"))
        self.assertEqual((events[-1]["reason"], events[-1]["task_state"]), ("stop", "completed"))
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(store.read(original.id), original_data)
        restored = store.restore(events[0]["session_id"], self.project)[1]
        self.assertIn("NotExecuted", json.dumps(restored))
        self.assertEqual(len(store.read(events[0]["session_id"])[2]), 1)

    def test_partial_batch_preserves_finished_write_without_replaying_it(self):
        reply = self.reply(("written", "write_file", {"path": "saved.txt", "content": "已保存"}),
                           ("pending", "write_file", {"path": "pending.txt", "content": "尚未执行"}))
        count = 0
        def decide(prompt):
            nonlocal count
            count += 1
            if count == 2:
                raise KeyboardInterrupt()
            return "y"
        source, _, _, _, _ = self.run_cli(["保存两份文件"], [reply], decide, error=True)
        plan = load_recovery(str(source), self.project)
        self.assertEqual([item["status"] for item in plan.tools], ["recorded", "not_executed"])
        self.assertTrue(json.loads(plan.history[-2]["content"])["ok"])
        self.assertEqual(json.loads(plan.history[-1]["content"])["error"]["type"], "NotExecuted")
        with patch("learn_agent.tools.write_file") as writer:
            _, _, _, approval, _ = self.run_cli(["--recover", str(source), "仅说明历史，不调用工具"], [self.final])
        writer.assert_not_called()
        approval.assert_not_called()
        self.assertEqual((self.project / "saved.txt").read_text(encoding="utf-8"), "已保存")
        self.assertFalse((self.project / "pending.txt").exists())

    def test_closed_confirmation_input_is_cancelled_and_recoverable(self):
        def closed(prompt):
            raise EOFError()
        source, model, _, _, output = self.run_cli(["运行脚本"], [self.reply()], closed, error=True)
        model.assert_called_once()
        self.assertEqual(self.entries(source)[-1]["reason"], "cancelled")
        self.assertIn("终端输入已关闭", output)
        load_recovery(str(source), self.project).require_safe()

    def test_request_errors_are_not_automatically_retried_but_explicit_recovery_works(self):
        errors = [TimeoutError(), urllib.error.URLError("offline"), urllib.error.HTTPError("offline", 429, "busy", {}, None)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                source, model, _, _, _ = self.run_cli(["回答问题"], [error], error=True)
                model.assert_called_once()
                self.assertEqual(self.entries(source)[-1]["reason"], "error")
                plan = load_recovery(str(source), self.project)
                self.assertFalse(plan.tools)
                self.assertIn("没有完整回复", plan.note)
                _, retried, _, approval, _ = self.run_cli(["--recover", str(source), "重新回答"], [self.final])
                retried.assert_called_once()
                approval.assert_not_called()

    def test_request_error_after_read_preserves_result_and_does_not_inherit_auto_read(self):
        reading = self.reply(("first_read", "read_file", {"path": "notes.txt"}))
        source, _, _, _, _ = self.run_cli(["--auto-read", "读资料后回答"], [reading, TimeoutError()], error=True)
        plan = load_recovery(str(source), self.project)
        self.assertIn("7391", json.dumps(plan.history))
        self.assertEqual(plan.tools[0]["status"], "recorded")
        new_reading = self.reply(("new_read", "read_file", {"path": "notes.txt"}))
        with patch("learn_agent.tools.read_file", wraps=tools.read_file) as reader:
            _, _, _, approval, _ = self.run_cli(["--recover", str(source), "再次读取"], [new_reading, self.final])
        reader.assert_called_once_with("notes.txt")
        approval.assert_called_once()

    def test_truncated_reply_is_discarded_and_never_executed(self):
        response = self.reply(("partial", "write_file", {"path": "bad.txt", "content": "不应写入"}))
        response["choices"][0]["finish_reason"] = "length"
        source, _, _, _, _ = self.run_cli(["生成内容"], [response])
        plan = load_recovery(str(source), self.project)
        self.assertEqual(len(plan.history), 1)
        self.assertIn("被截断", plan.note)
        with patch("learn_agent.tools.write_file") as writer:
            self.run_cli(["--recover", str(source), "重新回答，不写文件"], [self.final])
        writer.assert_not_called()
        self.assertFalse((self.project / "bad.txt").exists())

    def test_interrupted_write_may_have_effect_and_blocks_preview_and_recovery(self):
        original = tools.write_file
        def interrupted(path, content):
            original(path, content)
            raise KeyboardInterrupt()
        reply = self.reply(("write", "write_file", {"path": "changed.txt", "content": "已写入但未返回"}))
        with patch("learn_agent.tools.write_file", side_effect=interrupted):
            source, _, _, _, _ = self.run_cli(["写入文件"], [reply], error=True)
        self.assertEqual((self.project / "changed.txt").read_text(encoding="utf-8"), "已写入但未返回")
        plan = load_recovery(str(source), self.project)
        self.assertEqual(plan.tools[0]["status"], "unknown")
        with self.assertRaisesRegex(ValueError, "结果未知"):
            plan.require_safe()
        before = self.snapshot()
        for extra in ([], ["--preview"]):
            _, model, key, approval, output = self.run_cli(["--recover", str(source), *extra, "继续"], error=True)
            for operation in (model, key, approval):
                operation.assert_not_called()
            self.assertIn("结果未知", output)
        self.assertEqual(self.snapshot(), before)

    def test_legacy_approved_call_without_result_is_unknown(self):
        source = self.cancelled_source()
        entries = self.entries(source)
        approval = {**entries[-2], "event": "tool_approval", "approved": True, "source": "user", "task_state": "running"}
        entries.insert(-1, approval)
        new = self.write_log(entries, "approved-new.jsonl")
        load_recovery(str(new), self.project).require_safe()
        entries[0].pop("recovery_version")
        legacy = self.write_log(entries, "approved-legacy.jsonl")
        with self.assertRaisesRegex(ValueError, "结果未知"):
            load_recovery(str(legacy), self.project).require_safe()

    def test_legacy_waiting_cancel_without_approval_is_safe(self):
        entries = self.entries(self.cancelled_source())
        entries[0].pop("recovery_version")
        legacy = self.write_log(entries, "legacy.jsonl")
        plan = load_recovery(str(legacy), self.project)
        plan.require_safe()
        self.assertEqual(plan.tools[0]["status"], "not_executed")

    def test_failed_process_cleanup_stops_batch_and_blocks_recovery(self):
        reply = self.reply(("command", "run_command", {"command": ["python", "main.py"]}),
                           ("later", "write_file", {"path": "later.txt", "content": "不能写入"}))
        with patch("learn_agent.commands.run_command", return_value={"ok": False, "cleanup_succeeded": False}):
            source, model, _, approval, _ = self.run_cli(["执行命令"], [reply])
        model.assert_called_once()
        approval.assert_called_once()
        self.assertFalse((self.project / "later.txt").exists())
        self.assertEqual(self.entries(source)[-1]["task_state"], "failed")
        with self.assertRaisesRegex(ValueError, "清理未确认"):
            load_recovery(str(source), self.project).require_safe()

    def test_log_without_end_is_not_assumed_to_be_stopped(self):
        entries = self.entries(self.cancelled_source())[:-1]
        source = self.write_log(entries, "unfinished.jsonl")
        before = self.snapshot()
        _, model, key, approval, output = self.run_cli(["--recover", str(source), "继续"], error=True)
        for operation in (model, key, approval):
            operation.assert_not_called()
        self.assertIn("无法确认原任务已经停止", output)
        self.assertEqual(self.snapshot(), before)

    def test_malformed_logs_wrong_workspace_and_invalid_calls_fail_before_api(self):
        original = self.entries(self.cancelled_source())
        invalid = []
        wrong = copy.deepcopy(original)
        wrong[0]["workspace"] = str(self.root)
        invalid.append(wrong)
        bad = copy.deepcopy(original)
        bad[2]["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{unfinished"
        invalid.append(bad)
        orphan = copy.deepcopy(original)
        orphan.insert(3, {"event": "tool_started", "step": 1, "tool_call_id": "orphan", "task_state": "running"})
        invalid.append(orphan)
        invalid.extend([[], original[1:]])
        for index, entries in enumerate(invalid):
            with self.subTest(index=index):
                source = self.write_log(entries)
                _, model, key, approval, _ = self.run_cli(["--recover", str(source), "继续"], error=True)
                for operation in (model, key, approval):
                    operation.assert_not_called()
        source.write_text('{"event":', encoding="utf-8")
        self.run_cli(["--recover", str(source), "继续"], error=True)

    def test_recover_requires_new_prompt_and_is_mutually_exclusive_with_other_restore_modes(self):
        source = self.cancelled_source()
        for args in (["--recover", str(source)], ["--recover", str(source), "--resume", str(source), "继续"],
                     ["--recover", str(source), "--session", "some-id", "继续"],
                     ["--recover", str(source), "--status", str(source)], ["--recover", str(source), "--sessions"]):
            with self.subTest(args=args):
                before = self.snapshot()
                _, model, key, approval, _ = self.run_cli(args, error=True)
                for operation in (model, key, approval):
                    operation.assert_not_called()
                self.assertEqual(self.snapshot(), before)

    def test_normal_run_still_uses_original_restore_modes(self):
        source, _, _, _, _ = self.run_cli(["回答"], [self.final])
        _, model, key, approval, output = self.run_cli(["--recover", str(source), "继续"], error=True)
        for operation in (model, key, approval):
            operation.assert_not_called()
        self.assertIn("请使用 --resume", output)


if __name__ == "__main__":
    unittest.main()
