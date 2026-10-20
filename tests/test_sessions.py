import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call
from learn_agent import loop, tools
from learn_agent.sessions import Message, Session, SessionStore, ToolRun


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="learn-agent-session-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.project = self.root / "中文 项目"
        self.project.mkdir()
        (self.project / "notes.txt").write_text("项目名：小灯，实验编号：7391。", encoding="utf-8")
        (self.project / "main.py").write_text("print(5050)\n", encoding="utf-8")
        self.logs = self.root / "logs"
        self.store = SessionStore(self.logs / "sessions.sqlite3")
        log_patch = patch.object(tool_call, "LOG_DIR", self.logs)
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.final = {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "项目名是小灯，实验编号是 7391。",
        }}]}

    def reply(self, *calls):
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
                for call_id, name, arguments in calls
            ],
        }}]}

    def run_cli(self, arguments, replies=(), answers=(), workspace=None):
        argv = ["tool_call.py", "--workspace", str(workspace or self.project), *arguments]
        with patch.object(sys, "argv", argv), patch("tool_call.get_api_key", return_value="offline-private-key") as key:
            with patch("learn_agent.providers.call_model", side_effect=replies) as model:
                with patch("builtins.input", side_effect=answers) as approval, contextlib.redirect_stdout(io.StringIO()) as output:
                    tool_call.main()
        return model, key, approval, output.getvalue()

    def first_run(self):
        reads = self.reply(("notes", "read_file", {"path": "notes.txt"}),
                           ("code", "read_file", {"path": "main.py"}))
        self.run_cli(["--auto-read", "读取资料和代码"], [reads, self.final])
        return self.store.list_sessions()[0]

    def last_log(self):
        return max(self.logs.glob("*.jsonl"), key=lambda path: path.stat().st_mtime_ns)

    def test_completed_turn_has_typed_records_and_versioned_sqlite_tables(self):
        saved = self.first_run()
        session, messages, runs = self.store.read(saved.id)
        self.assertIsInstance(session, Session)
        self.assertTrue(all(isinstance(message, Message) for message in messages))
        self.assertTrue(all(isinstance(run, ToolRun) for run in runs))
        self.assertEqual(session.workspace, str(self.project.resolve()))
        self.assertEqual(session.reason, "stop")
        self.assertEqual([message.role for message in messages], ["user", "assistant", "tool", "tool", "assistant"])
        self.assertEqual([message.position for message in messages], list(range(5)))
        self.assertEqual(messages[1].tool_calls[0]["id"], "notes")
        self.assertIn("7391", messages[2].content)
        self.assertEqual([run.name for run in runs], ["read_file", "read_file"])
        self.assertTrue(all(run.approved and run.approval_source == "auto_read" for run in runs))
        self.assertEqual(runs[0].result["content"], "项目名：小灯，实验编号：7391。")
        self.assertEqual(json.loads(runs[0].arguments), {"path": "notes.txt"})
        self.assertNotIn(b"offline-private-key", self.store.path.read_bytes())
        with contextlib.closing(sqlite3.connect(self.store.path)) as database, database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT count(*) FROM messages").fetchone()[0], 5)
            self.assertEqual(database.execute("SELECT count(*) FROM tool_runs").fetchone()[0], 2)
        entries = [json.loads(line) for line in self.last_log().read_text(encoding="utf-8").splitlines()]
        self.assertEqual(entries[0]["session_id"], saved.id)

    def test_new_process_reads_database_after_original_jsonl_is_removed(self):
        saved = self.first_run()
        Path(saved.last_log).unlink()
        script = (
            "import json,sys; from pathlib import Path; from learn_agent.sessions import SessionStore; "
            "store=SessionStore(Path(sys.argv[1])); s,m,t=store.read(sys.argv[2]); "
            "print(json.dumps({'id':s.id,'messages':len(m),'tools':len(t),'result':t[0].result},ensure_ascii=False))"
        )
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script, str(self.store.path), saved.id],
                                capture_output=True, text=True, encoding="utf-8", timeout=10,
                                env={key: value for key, value in os.environ.items() if key.upper() != "DEEPSEEK_API_KEY"})
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual((data["id"], data["messages"], data["tools"]), (saved.id, 5, 2))
        self.assertIn("7391", data["result"]["content"])
        _, history = self.store.restore(saved.id, self.project)
        self.assertEqual(len(history), 5)

    def test_continuation_keeps_id_and_appends_without_duplicating_history(self):
        saved = self.first_run()
        original_log = Path(saved.last_log).read_bytes()
        for question in ("项目名是什么？", "实验编号是什么？"):
            previous = self.store.read(saved.id)[1]
            with patch("learn_agent.tools.read_file") as reader, patch("learn_agent.tools.run_python") as runner:
                model, _, approval, output = self.run_cli(["--session", saved.id, question], [self.final])
            reader.assert_not_called()
            runner.assert_not_called()
            approval.assert_not_called()
            request = model.call_args.args[0]
            self.assertEqual(request["messages"][1:-1], [message.to_api() for message in previous])
            self.assertEqual(request["messages"][-1], {"role": "user", "content": question})
            self.assertIn(f"[会话已保存] {saved.id}", output)
        session, messages, runs = self.store.read(saved.id)
        self.assertEqual(len(self.store.list_sessions()), 1)
        self.assertEqual((len(messages), len(runs)), (9, 2))
        self.assertNotEqual(session.last_log, saved.last_log)
        self.assertEqual(Path(saved.last_log).read_bytes(), original_log)

    def test_preview_reads_sqlite_without_credentials_new_logs_or_database_writes(self):
        saved = self.first_run()
        original = {path: path.read_bytes() for path in self.logs.iterdir()}
        model, key, approval, output = self.run_cli(["--session", saved.id, "--preview", "只根据历史回答"])
        for operation in (model, key, approval):
            operation.assert_not_called()
        payload, _ = json.JSONDecoder().raw_decode(output.split("\n", 1)[1])
        self.assertIn("7391", json.dumps(payload["messages"], ensure_ascii=False))
        self.assertEqual({path: path.read_bytes() for path in self.logs.iterdir()}, original)

    def test_empty_listing_and_plain_preview_do_not_create_database(self):
        for arguments in (["--sessions"], ["--preview", "预览"]):
            model, key, approval, _ = self.run_cli(arguments)
            for operation in (model, key, approval):
                operation.assert_not_called()
        self.assertFalse(self.logs.exists())

    def test_listing_existing_sessions_does_not_invoke_api_or_mutate_database(self):
        saved = self.first_run()
        before = self.store.path.read_bytes()
        model, key, approval, output = self.run_cli(["--sessions"])
        self.assertIn(saved.id, output)
        self.assertIn(str(self.project.resolve()), output)
        for operation in (model, key, approval):
            operation.assert_not_called()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_unknown_id_and_wrong_workspace_fail_before_credentials_or_api(self):
        saved = self.first_run()
        other = self.root / "other"
        other.mkdir()
        before = self.store.path.read_bytes()
        for session_id, workspace in (("missing", self.project), (saved.id, other)):
            with self.subTest(session=session_id, workspace=workspace):
                with patch("tool_call.get_api_key") as key, patch("learn_agent.providers.call_model") as model:
                    with patch.object(sys, "argv", ["tool_call.py", "--workspace", str(workspace), "--session", session_id, "继续"]):
                        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                            tool_call.main()
                    key.assert_not_called()
                    model.assert_not_called()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_automatic_read_permission_is_not_inherited(self):
        saved = self.first_run()
        reading = self.reply(("read_again", "read_file", {"path": "notes.txt"}))
        with patch("learn_agent.tools.read_file", wraps=tools.read_file) as reader:
            _, _, approval, _ = self.run_cli(["--session", saved.id, "重新读取"], [reading, self.final], ["y"])
        reader.assert_called_once_with("notes.txt")
        approval.assert_called_once()
        runs = self.store.read(saved.id)[2]
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs[-1].approval_source, "user")

    def test_actual_subprocess_result_and_user_approval_are_stored(self):
        running = self.reply(("python", "run_python", {"path": "main.py"}))
        self.run_cli(["运行代码"], [running, self.final], ["y"])
        session = self.store.list_sessions()[0]
        run = self.store.read(session.id)[2][0]
        self.assertTrue(run.approved)
        self.assertEqual(run.approval_source, "user")
        self.assertEqual((run.result["exit_code"], run.result["stdout"]), (0, "5050\n"))

    def test_denied_tool_is_recorded_without_fake_execution_result(self):
        running = self.reply(("denied", "run_python", {"path": "main.py"}))
        with patch("learn_agent.tools.run_python") as runner:
            self.run_cli(["运行代码"], [running], ["n"])
        runner.assert_not_called()
        session = self.store.list_sessions()[0]
        run = self.store.read(session.id)[2][0]
        self.assertEqual(session.reason, "user_denied")
        self.assertFalse(run.approved)
        self.assertIsNone(run.result)
        with self.assertRaises(ValueError):
            self.store.restore(session.id, self.project)

    def test_limit_repair_is_saved_as_message_but_not_as_executed_tool(self):
        running = self.reply(("pending", "run_python", {"path": "main.py"}))
        with patch.object(loop, "MAX_MODEL_CALLS", 1), patch("learn_agent.tools.run_python") as runner:
            self.run_cli(["运行代码"], [running])
        runner.assert_not_called()
        saved = self.store.list_sessions()[0]
        session, history = self.store.restore(saved.id, self.project)
        self.assertEqual(session.reason, "max_model_calls")
        self.assertEqual(json.loads(history[-1]["content"])["error"]["type"], "NotExecuted")
        run = self.store.read(saved.id)[2][0]
        self.assertIsNone(run.approved)
        self.assertIsNone(run.result)
        self.run_cli(["--session", saved.id, "只根据历史说明状态"], [self.final])
        self.assertEqual(len(self.store.read(saved.id)[2]), 1)

    def test_error_cancel_and_truncated_turns_are_saved_but_not_resumed(self):
        replies = ([ValueError("request failed")], [KeyboardInterrupt()], [{"choices": [{
            "finish_reason": "length", "message": {"role": "assistant", "content": "partial"},
        }]}])
        for reply, reason in zip(replies, ("error", "cancelled", "abnormal_finish")):
            with self.subTest(reason=reason):
                if reason == "abnormal_finish":
                    self.run_cli(["回答"], reply)
                else:
                    with self.assertRaises(SystemExit):
                        self.run_cli(["回答"], reply)
                saved = self.store.list_sessions()[0]
                self.assertEqual(saved.reason, reason)
                self.assertTrue(self.store.read(saved.id)[1])
                with self.assertRaises(ValueError):
                    self.store.restore(saved.id, self.project)

    def test_failed_tool_result_is_structured_and_resumable_after_final_stop(self):
        missing = self.reply(("missing", "read_file", {"path": "missing.txt"}))
        self.run_cli(["--auto-read", "读文件"], [missing, self.final])
        saved = self.store.list_sessions()[0]
        run = self.store.read(saved.id)[2][0]
        self.assertFalse(run.result["ok"])
        self.assertEqual(run.result["error"]["type"], "FileNotFoundError")
        self.assertIn("FileNotFoundError", json.dumps(self.store.restore(saved.id, self.project)[1]))

    def test_jsonl_resume_remains_compatible_and_creates_a_new_session(self):
        saved = self.first_run()
        before = self.store.read(saved.id)
        self.run_cli(["--resume", saved.last_log, "根据历史回答"], [self.final])
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 2)
        self.assertEqual(self.store.read(saved.id), before)
        other = next(session for session in sessions if session.id != saved.id)
        self.assertEqual(len(self.store.read(other.id)[1]), 7)

    def test_sql_failure_rolls_back_session_messages_and_tools_together(self):
        saved = self.first_run()
        before = self.store.read(saved.id)
        with contextlib.closing(sqlite3.connect(self.store.path)) as database, database:
            database.execute("CREATE TRIGGER fail_tool BEFORE INSERT ON tool_runs BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        reading = self.reply(("new_read", "read_file", {"path": "notes.txt"}))
        with self.assertRaisesRegex(SystemExit, "JSONL 日志保留"):
            self.run_cli(["--auto-read", "--session", saved.id, "再读一次"], [reading, self.final])
        self.assertEqual(self.store.read(saved.id), before)
        events = [json.loads(line) for line in self.last_log().read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["reason"], "stop")

    def test_stale_writer_cannot_replace_newer_session_history(self):
        saved = self.first_run()
        self.run_cli(["--session", saved.id, "继续"], [self.final])
        before = self.store.read(saved.id)
        with self.assertRaisesRegex(ValueError, "其他运行更新"):
            self.store.save_run(saved.id, Path(saved.last_log), expected_log=saved.last_log)
        self.assertEqual(self.store.read(saved.id), before)

    def test_unknown_schema_version_is_rejected_before_credentials(self):
        self.store.initialize()
        with contextlib.closing(sqlite3.connect(self.store.path)) as database, database:
            database.execute("PRAGMA user_version = 99")
        before = self.store.path.read_bytes()
        with patch("tool_call.get_api_key") as key, patch("learn_agent.providers.call_model") as model:
            with patch.object(sys, "argv", ["tool_call.py", "--workspace", str(self.project), "回答"]):
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(SystemExit, "版本 99"):
                    tool_call.main()
        key.assert_not_called()
        model.assert_not_called()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_unversioned_existing_database_is_not_overwritten(self):
        self.logs.mkdir()
        with contextlib.closing(sqlite3.connect(self.store.path)) as database, database:
            database.execute("CREATE TABLE personal (value TEXT)")
            database.execute("INSERT INTO personal VALUES ('keep')")
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "没有 learn-agent 版本"):
            self.store.initialize()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_corrupted_tool_pair_is_rejected_before_model_call(self):
        saved = self.first_run()
        with contextlib.closing(sqlite3.connect(self.store.path)) as database, database:
            database.execute("UPDATE messages SET tool_call_id = 'unknown' WHERE role = 'tool'")
        with self.assertRaisesRegex(ValueError, "没有对应的请求"):
            self.store.restore(saved.id, self.project)

    def test_incomplete_log_is_not_published_as_session(self):
        self.logs.mkdir()
        log = self.logs / "incomplete.jsonl"
        log.write_text(json.dumps({"event": "run_start"}) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "日志不完整"):
            self.store.save_run("incomplete", log)
        self.assertFalse(self.store.path.exists())

    def test_session_flags_require_a_task_and_do_not_mix_with_log_resume(self):
        cases = (["--session", "id"], ["--session", "id", "--resume", "file", "任务"],
                 ["--sessions", "任务"], ["--sessions", "--preview"], ["--sessions", "--session", "id"])
        for arguments in cases:
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_cli(arguments)
        self.assertFalse(self.logs.exists())

    def test_empty_session_id_is_rejected_without_credentials_api_or_storage(self):
        for session_id in ("", "   ", "\t"):
            for preview in (False, True):
                with self.subTest(session_id=repr(session_id), preview=preview):
                    arguments = ["tool_call.py", "--workspace", str(self.project), "--session", session_id]
                    if preview:
                        arguments.append("--preview")
                    arguments.append("根据历史回答")
                    with patch.object(sys, "argv", arguments), patch("tool_call.get_api_key") as key:
                        with patch("learn_agent.providers.call_model") as model:
                            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as error:
                                with self.assertRaises(SystemExit) as stopped:
                                    tool_call.main()
                    self.assertEqual(stopped.exception.code, 2)
                    self.assertIn("会话 ID 不能为空", error.getvalue())
                    key.assert_not_called()
                    model.assert_not_called()
                    self.assertFalse(self.logs.exists())


if __name__ == "__main__":
    unittest.main()
