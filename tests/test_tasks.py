import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call
from learn_agent import loop
from learn_agent.events import EventRecorder, record_event
from learn_agent.sessions import SessionStore
from learn_agent.tasks import replay_events


class TaskTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-task-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "main.py").write_text("print(5050)\n", encoding="utf-8")
        self.logs = self.root / "logs"
        log_patch = patch.object(tool_call, "LOG_DIR", self.logs)
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.final = {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "已完成。",
        }}]}

    def call(self, name="run_python", arguments=None, call_id="call_1"):
        return {"id": call_id, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments or {"path": "main.py"}),
        }}

    def response(self, *calls):
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "tool_calls": list(calls or [self.call()]),
        }}]}

    def run_cli(self, responses, answers="y", extra=(), raises=False):
        output = io.StringIO()
        args = ["tool_call.py", "--workspace", str(self.project), *extra, "验证任务状态"]
        with patch.object(sys, "argv", args), patch("tool_call.get_api_key", return_value="offline-key"):
            with patch("learn_agent.loop.call_model", side_effect=responses) as model:
                with patch("builtins.input", side_effect=answers if callable(answers) else None,
                           return_value=answers if not callable(answers) else None) as approval:
                    with contextlib.redirect_stdout(output):
                        if raises:
                            with self.assertRaises(SystemExit):
                                tool_call.main()
                        else:
                            tool_call.main()
        log_path = next(self.logs.glob("*.jsonl"))
        entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        return entries, model, approval, output.getvalue(), log_path

    def test_confirmed_run_records_waiting_before_input_and_completes(self):
        def approve(prompt):
            entries = [json.loads(line) for line in next(self.logs.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
            self.assertEqual((entries[-1]["event"], entries[-1]["task_state"]), ("tool_waiting", "waiting_approval"))
            self.assertFalse(any(entry["event"] == "tool_result" for entry in entries))
            return "y"

        entries, model, approval, output, _ = self.run_cli([self.response(), self.final], approve)
        status, transitions = replay_events(entries)
        self.assertEqual([state for _, state in transitions], ["running", "waiting_approval", "running", "completed"])
        self.assertEqual((status.state, status.reason, status.ended), ("completed", "stop", True))
        self.assertEqual(model.call_count, 2)
        approval.assert_called_once()
        result = next(entry["result"] for entry in entries if entry["event"] == "tool_result")
        self.assertEqual((result["exit_code"], result["stdout"].strip()), (0, "5050"))
        self.assertIn("[任务状态] waiting_approval", output)
        self.assertIn("[任务状态] completed", output)
        self.assertTrue(all("task_state" in entry for entry in entries))

    def test_denial_cancels_remaining_batch_and_model_requests(self):
        calls = [self.call(), self.call("write_file", {"path": "unwanted.txt", "content": "不能写入"}, "call_2")]
        with patch("learn_agent.tools.run_python") as runner:
            entries, model, approval, _, _ = self.run_cli([self.response(*calls)], "n")
        runner.assert_not_called()
        model.assert_called_once()
        approval.assert_called_once()
        self.assertFalse((self.project / "unwanted.txt").exists())
        self.assertNotIn("tool_result", [entry["event"] for entry in entries])
        status, transitions = replay_events(entries)
        self.assertEqual([state for _, state in transitions], ["running", "waiting_approval", "cancelled"])
        self.assertEqual((status.reason, status.ended), ("user_denied", True))

    def test_auto_read_does_not_pretend_to_wait_for_confirmation(self):
        entries, _, approval, _, _ = self.run_cli([
            self.response(self.call("read_file")), self.final,
        ], extra=["--auto-read"])
        approval.assert_not_called()
        self.assertNotIn("tool_waiting", [entry["event"] for entry in entries])
        self.assertEqual([state for _, state in replay_events(entries)[1]], ["running", "completed"])
        decision = next(entry for entry in entries if entry["event"] == "tool_approval")
        self.assertEqual(decision["source"], "auto_read")

    def test_tool_failure_is_observation_and_task_can_still_finish(self):
        entries, model, _, _, _ = self.run_cli([
            self.response(self.call("read_file", {"path": "missing.txt"})), self.final,
        ], extra=["--auto-read"])
        result = next(entry for entry in entries if entry["event"] == "tool_result")
        self.assertFalse(result["result"]["ok"])
        self.assertEqual(result["task_state"], "running")
        self.assertEqual(replay_events(entries)[0].state, "completed")
        self.assertEqual(model.call_count, 2)

    def test_request_limit_pauses_before_any_approval_or_execution(self):
        with patch.object(loop, "MAX_MODEL_CALLS", 1), patch("learn_agent.tools.run_python") as runner:
            entries, model, approval, _, _ = self.run_cli([self.response()])
        runner.assert_not_called()
        approval.assert_not_called()
        model.assert_called_once()
        status, _ = replay_events(entries)
        self.assertEqual((status.state, status.reason), ("paused", "max_model_calls"))

    def test_abnormal_model_finish_fails_without_executing_tools(self):
        response = self.response()
        response["choices"][0]["finish_reason"] = "length"
        with patch("learn_agent.tools.run_python") as runner:
            entries, _, approval, _, _ = self.run_cli([response])
        runner.assert_not_called()
        approval.assert_not_called()
        status, _ = replay_events(entries)
        self.assertEqual((status.state, status.reason), ("failed", "abnormal_finish"))

    def test_request_exception_fails_and_interrupt_cancels(self):
        for error, state in [(TimeoutError("private details"), "failed"), (KeyboardInterrupt(), "cancelled")]:
            with self.subTest(error=type(error).__name__):
                self.logs = self.root / state
                with patch.object(tool_call, "LOG_DIR", self.logs):
                    entries, _, _, output, _ = self.run_cli([error], raises=True)
                status, _ = replay_events(entries)
                self.assertEqual(status.state, state)
                self.assertEqual(entries[-1]["error_type"], type(error).__name__)
                self.assertNotIn("private details", output)

    def test_interrupt_while_waiting_starts_no_tool_or_next_request(self):
        def interrupt(prompt):
            raise KeyboardInterrupt()

        with patch("learn_agent.tools.run_python") as runner:
            entries, model, _, _, _ = self.run_cli([self.response()], interrupt, raises=True)
        runner.assert_not_called()
        model.assert_called_once()
        status, transitions = replay_events(entries)
        self.assertEqual([state for _, state in transitions], ["running", "waiting_approval", "cancelled"])
        self.assertEqual(status.reason, "cancelled")

    def test_finished_and_denied_recorders_block_followup_before_model_call(self):
        for end in ("stop", "error", "abnormal_finish", "cancelled", "max_model_calls", "user_denied", None):
            with self.subTest(reason=end), contextlib.redirect_stdout(io.StringIO()):
                text = io.StringIO()
                events = EventRecorder(text)
                record_event(events, "run_start")
                if end is None:
                    record_event(events, "tool_waiting")
                    record_event(events, "tool_approval", approved=False)
                else:
                    record_event(events, "run_end", reason=end)
                before = text.getvalue()
                with patch("learn_agent.loop.call_model") as model:
                    with self.assertRaises(ValueError):
                        loop.run_agent({"messages": []}, "offline-key", events)
                model.assert_not_called()
                self.assertEqual(text.getvalue(), before)

    def test_status_is_read_only_and_works_without_api_key(self):
        entries, _, _, _, log_path = self.run_cli([self.response(), self.final])
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        output = io.StringIO()
        with patch.object(sys, "argv", ["tool_call.py", "--status", str(log_path)]):
            with patch("tool_call.get_api_key") as key, patch("learn_agent.loop.call_model") as model:
                with patch("builtins.input") as approval, contextlib.redirect_stdout(output):
                    tool_call.main()
        key.assert_not_called()
        model.assert_not_called()
        approval.assert_not_called()
        self.assertIn("[当前状态] completed", output.getvalue())
        self.assertIn("[结束原因] stop", output.getvalue())
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        self.assertEqual(replay_events(entries)[0].state, "completed")

    def test_legacy_events_without_state_or_waiting_still_replay(self):
        entries, _, _, _, _ = self.run_cli([self.response(), self.final])
        legacy = [{key: value for key, value in entry.items() if key != "task_state"}
                  for entry in entries if entry["event"] != "tool_waiting"]
        status, _ = replay_events(legacy)
        self.assertEqual((status.state, status.reason), ("completed", "stop"))

    def test_incomplete_log_reports_last_saved_state_without_claiming_liveness(self):
        entries, _, _, _, _ = self.run_cli([self.response(), self.final])
        cut = next(i for i, entry in enumerate(entries) if entry["event"] == "tool_waiting")
        prefix = entries[:cut + 1]
        denied = [*prefix, {"event": "tool_approval", "approved": False}]
        for records, expected in [(prefix, "waiting_approval"), (denied, "cancelled")]:
            with self.subTest(state=expected):
                status, _ = replay_events(records)
                self.assertEqual((status.state, status.reason, status.ended), (expected, None, False))
                path = self.root / "unfinished.jsonl"
                path.write_text("\n".join(json.dumps(entry) for entry in records) + "\n", encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    tool_call.show_task_status(str(path))
                self.assertIn("尚无 run_end", output.getvalue())
                self.assertIn("不能据此确认进程仍在运行", output.getvalue())

    def test_invalid_status_history_is_rejected(self):
        entries, _, _, _, _ = self.run_cli([self.final])
        mismatch = copy.deepcopy(entries)
        mismatch[-1]["task_state"] = "running"
        for invalid in ([], entries[1:], [*entries, entries[1]], [*entries[:1], *entries], mismatch):
            with self.subTest(events=invalid), self.assertRaises(ValueError):
                replay_events(invalid)
        with self.assertRaises(ValueError):
            replay_events([entries[0], {"event": "tool_waiting"}, {"event": "model_request"}])

    def test_malformed_status_file_and_conflicting_flags_fail_before_api_or_storage(self):
        bad = self.root / "bad.jsonl"
        bad.write_text('{"event":', encoding="utf-8")
        for args in (["--status", str(bad)], ["--status", str(bad), "extra prompt"],
                     ["--status", str(bad), "--session", "some-id"], ["--status", str(bad), "--preview"]):
            with self.subTest(args=args), patch.object(sys, "argv", ["tool_call.py", *args]):
                with patch("tool_call.get_api_key") as key, patch("learn_agent.loop.call_model") as model:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit):
                            tool_call.main()
                key.assert_not_called()
                model.assert_not_called()
                self.assertFalse(self.logs.exists())

    def test_status_records_do_not_change_session_restore_or_replay_old_tools(self):
        _, _, _, _, _ = self.run_cli([self.response(), self.final])
        store = SessionStore(self.logs / "sessions.sqlite3")
        session = store.list_sessions()[0]
        saved, history = store.restore(session.id, self.project)
        self.assertEqual(saved.reason, "stop")
        self.assertEqual(len(history), 4)
        self.assertEqual(len(store.read(session.id)[2]), 1)
        self.assertEqual((self.project / "main.py").read_text(encoding="utf-8"), "print(5050)\n")


if __name__ == "__main__":
    unittest.main()
