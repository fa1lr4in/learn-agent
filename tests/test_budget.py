import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call
from learn_agent.budget import RunBudget
from learn_agent.model import call_model
from learn_agent.recovery import load_recovery
from learn_agent.sessions import SessionStore
from learn_agent.tasks import replay_events


class BudgetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-budget-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "中文 项目"
        self.project.mkdir()
        (self.project / "notes.txt").write_text("项目：小灯", encoding="utf-8")
        (self.project / "main.py").write_text("print(5050)\n", encoding="utf-8")
        self.logs = self.root / "logs"
        patcher = patch.object(tool_call, "LOG_DIR", self.logs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def reply(self, name=None, usage=None, finish=None):
        message = {"role": "assistant", "content": "已完成。" if name is None else ""}
        if name:
            message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {
                "name": name, "arguments": json.dumps({"path": "main.py" if name == "run_python" else "notes.txt"}),
            }}]
        return {"choices": [{"message": message, "finish_reason": finish or ("tool_calls" if name else "stop")}],
                **({"usage": usage} if usage is not None else {})}

    def run_cli(self, replies=(), args=(), answer="y", error=False):
        before = set(self.logs.glob("*.jsonl"))
        output = io.StringIO()
        with patch.object(sys, "argv", ["tool_call.py", "--workspace", str(self.project), *args, "验证预算"]):
            with patch("tool_call.get_api_key", return_value="offline-private-budget-key") as key:
                with patch("learn_agent.providers.call_model", side_effect=replies) as model, patch("builtins.input", side_effect=answer if callable(answer) else None, return_value=answer) as approval:
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                        if error:
                            with self.assertRaises(SystemExit):
                                tool_call.main()
                        else:
                            tool_call.main()
        paths = set(self.logs.glob("*.jsonl")) - before
        path = next(iter(paths)) if paths else None
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path else []
        return path, entries, output.getvalue(), model, key, approval

    def usage(self, prompt=100, completion=40):
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
                "prompt_cache_hit_tokens": prompt - 10, "prompt_cache_miss_tokens": 10}

    def test_two_requests_sum_reported_usage_once_including_repeated_history(self):
        _, entries, output, model, _, approval = self.run_cli([
            self.reply("run_python", self.usage()), self.reply(usage=self.usage(50, 10)),
        ], args=["--max-total-tokens", "1000"])
        summary = entries[-1]["summary"]
        self.assertEqual(summary["usage"], {"prompt_tokens": 150, "completion_tokens": 50, "total_tokens": 200})
        self.assertTrue(summary["usage_complete"])
        self.assertEqual((summary["model_calls"], summary["responses_received"]), (2, 2))
        self.assertGreaterEqual(summary["elapsed_seconds"], 0)
        self.assertEqual(entries[-1]["reason"], "stop")
        self.assertEqual(next(e for e in entries if e["event"] == "tool_result")["result"]["stdout"], "5050\n")
        self.assertIn("合计 token：200", output)
        self.assertNotIn("offline-private-budget-key", json.dumps(entries))
        self.assertEqual(model.call_count, 2)
        approval.assert_called_once()

    def test_equal_or_exceeded_threshold_pauses_before_tool_preparation(self):
        for limit in (140, 1):
            with self.subTest(limit=limit), patch("learn_agent.tools.run_python") as runner:
                path, entries, _, model, _, approval = self.run_cli([
                    self.reply("run_python", self.usage()),
                ], args=["--max-total-tokens", str(limit)])
                runner.assert_not_called()
                approval.assert_not_called()
                model.assert_called_once()
                self.assertEqual((entries[-1]["reason"], replay_events(entries)[0].state), ("max_total_tokens", "paused"))
                self.assertEqual([e["event"] for e in entries], ["run_start", "model_request", "model_response", "run_end"])
                plan = load_recovery(str(path), self.project)
                plan.require_safe()
                self.assertEqual(plan.tools[0]["status"], "not_executed")
                self.assertIn("NotExecuted", plan.history[-1]["content"])

    def test_accumulated_threshold_stops_next_batch_but_keeps_prior_results(self):
        path, entries, _, model, _, approval = self.run_cli([
            self.reply("read_file", self.usage()), self.reply("run_python", self.usage(30, 15)),
        ], args=["--auto-read", "--max-total-tokens", "150"])
        self.assertEqual(entries[-1]["summary"]["usage"]["total_tokens"], 185)
        self.assertEqual(entries[-1]["reason"], "max_total_tokens")
        self.assertEqual([e["tool"] for e in entries if e["event"] == "tool_result"], ["read_file"])
        self.assertEqual(model.call_count, 2)
        approval.assert_not_called()
        plan = load_recovery(str(path), self.project)
        self.assertEqual([t["status"] for t in plan.tools], ["recorded", "not_executed"])
        self.assertIn("小灯", json.dumps(plan.history, ensure_ascii=False))

    def test_explicit_request_limit_keeps_existing_stop_reason_and_repair(self):
        _, entries, _, model, _, approval = self.run_cli([
            self.reply("run_python", self.usage()),
        ], args=["--max-model-calls", "1"])
        self.assertEqual(entries[0]["max_model_calls"], 1)
        self.assertEqual(entries[-1]["reason"], "max_model_calls")
        model.assert_called_once()
        approval.assert_not_called()
        store = SessionStore(self.logs / "sessions.sqlite3")
        _, history = store.restore(store.list_sessions()[0].id, self.project)
        self.assertIn("NotExecuted", history[-1]["content"])

    def test_missing_usage_without_token_limit_remains_usable_but_unknown(self):
        _, entries, output, _, _, _ = self.run_cli([self.reply("read_file"), self.reply(usage=self.usage())], args=["--auto-read"])
        summary = entries[-1]["summary"]
        self.assertFalse(summary["usage_complete"])
        self.assertTrue(all(value is None for value in summary["usage"].values()))
        self.assertEqual(summary["reported_usage"]["total_tokens"], 140)
        self.assertEqual(summary["usage_reports"]["total_tokens"], 1)
        self.assertIn("合计 token：未知", output)
        self.assertEqual(entries[-1]["reason"], "stop")

    def test_missing_or_invalid_total_with_threshold_pauses_and_is_recoverable(self):
        for usage in (None, {}, {"total_tokens": True}, {"total_tokens": -1}, {"total_tokens": "140"}, []):
            with self.subTest(usage=usage):
                path, entries, _, _, _, approval = self.run_cli([
                    self.reply("run_python", usage),
                ], args=["--max-total-tokens", "1000"])
                self.assertEqual(entries[-1]["reason"], "usage_unavailable")
                self.assertEqual(replay_events(entries)[0].state, "paused")
                self.assertIsNone(entries[-1]["summary"]["usage"]["total_tokens"])
                approval.assert_not_called()
                load_recovery(str(path), self.project).require_safe()

    def test_valid_total_allows_threshold_check_without_inventing_breakdown(self):
        _, entries, _, _, _, _ = self.run_cli([
            self.reply("read_file", {"total_tokens": 140}), self.reply(usage={"total_tokens": 60}),
        ], args=["--auto-read", "--max-total-tokens", "1000"])
        summary = entries[-1]["summary"]
        self.assertEqual(summary["usage"], {"prompt_tokens": None, "completion_tokens": None, "total_tokens": 200})
        self.assertFalse(summary["usage_complete"])
        self.assertEqual(entries[-1]["reason"], "stop")

    def test_complete_final_answer_still_completes_when_threshold_is_crossed(self):
        for usage in (self.usage(), None):
            with self.subTest(usage=usage):
                _, entries, _, _, _, _ = self.run_cli([self.reply(usage=usage)], args=["--max-total-tokens", "1"])
                self.assertEqual((entries[-1]["reason"], entries[-1]["task_state"]), ("stop", "completed"))

    def test_length_is_failure_even_when_token_threshold_is_crossed(self):
        _, entries, _, _, _, approval = self.run_cli([
            self.reply("run_python", self.usage(), finish="length"),
        ], args=["--max-total-tokens", "1"])
        self.assertEqual(entries[-1]["reason"], "abnormal_finish")
        self.assertEqual(entries[-1]["summary"]["usage"]["total_tokens"], 140)
        approval.assert_not_called()

    def test_request_error_or_cancel_keeps_attempt_count_and_unknown_usage(self):
        for error, reason in ((TimeoutError("timeout"), "error"), (KeyboardInterrupt(), "cancelled")):
            with self.subTest(reason=reason):
                _, entries, _, _, _, _ = self.run_cli([
                    self.reply("read_file", self.usage()), error,
                ], args=["--auto-read"], error=True)
                summary = entries[-1]["summary"]
                self.assertEqual((summary["model_calls"], summary["responses_received"]), (2, 1))
                self.assertEqual(summary["reported_usage"]["total_tokens"], 140)
                self.assertIsNone(summary["usage"]["total_tokens"])
                self.assertEqual(entries[-1]["reason"], reason)

    def test_denial_keeps_usage_and_does_not_spend_another_model_request(self):
        _, entries, _, model, _, _ = self.run_cli([self.reply("run_python", self.usage())], answer="n")
        self.assertEqual(entries[-1]["reason"], "user_denied")
        self.assertEqual(entries[-1]["summary"]["usage"]["total_tokens"], 140)
        model.assert_called_once()

    def test_budget_recovery_preview_and_new_budget_do_not_replay_old_tools(self):
        path, _, _, _, _, _ = self.run_cli([self.reply("run_python", self.usage())], args=["--max-total-tokens", "1"])
        before = {p: p.read_bytes() for p in self.logs.iterdir()}
        _, entries, output, model, key, approval = self.run_cli(args=["--preview", "--recover", str(path)])
        self.assertEqual(entries, [])
        self.assertIn("NotExecuted", output)
        for operation in (model, key, approval):
            operation.assert_not_called()
        self.assertEqual(before, {p: p.read_bytes() for p in self.logs.iterdir()})
        _, entries, _, model, _, approval = self.run_cli([
            self.reply("run_python", self.usage()), self.reply(usage=self.usage(50, 10)),
        ], args=["--recover", str(path), "--max-total-tokens", "1000"])
        self.assertEqual(entries[0]["max_total_tokens"], 1000)
        self.assertEqual(entries[-1]["summary"]["usage"]["total_tokens"], 200)
        self.assertEqual(entries[-1]["reason"], "stop")
        self.assertEqual(model.call_count, 2)
        approval.assert_called_once()
        self.assertEqual(path.read_bytes(), before[path])

    def test_session_continuation_starts_new_usage_and_default_limits(self):
        self.run_cli([self.reply(usage=self.usage())], args=["--max-model-calls", "1", "--max-total-tokens", "1"])
        store = SessionStore(self.logs / "sessions.sqlite3")
        saved = store.list_sessions()[0]
        _, entries, _, _, _, _ = self.run_cli([self.reply(usage=self.usage(50, 10))], args=["--session", saved.id])
        self.assertEqual(entries[0]["session_id"], saved.id)
        self.assertEqual((entries[0]["max_model_calls"], entries[0]["max_total_tokens"]), (8, None))
        self.assertEqual(entries[-1]["summary"]["usage"]["total_tokens"], 60)

    def test_invalid_limits_and_preview_do_not_read_key_or_create_logs(self):
        for name in ("--max-model-calls", "--max-total-tokens"):
            for value in ("0", "-1", "1.5", "abc"):
                with self.subTest(name=name, value=value):
                    _, _, _, model, key, approval = self.run_cli(args=[name, value], error=True)
                    for operation in (model, key, approval):
                        operation.assert_not_called()
        _, _, output, model, key, _ = self.run_cli(args=["--preview", "--max-model-calls", "2", "--max-total-tokens", "500"])
        self.assertIn("模型请求上限 2 次", output)
        model.assert_not_called()
        key.assert_not_called()
        self.assertFalse(self.logs.exists())

    def test_elapsed_time_uses_monotonic_and_includes_approval_wait(self):
        clock = [10]

        def approve(prompt):
            clock[0] = 15
            return "y"

        with patch("learn_agent.budget.monotonic", side_effect=lambda: clock[0]):
            _, entries, _, _, _, _ = self.run_cli([
                self.reply("run_python", self.usage()), self.reply(usage=self.usage()),
            ], answer=approve)
        self.assertEqual(entries[-1]["summary"]["elapsed_seconds"], 5)

    def test_budget_object_rejects_invalid_values(self):
        for calls, tokens in ((None, None), (0, None), (True, None), (8, 0), (8, False)):
            with self.subTest(calls=calls, tokens=tokens), self.assertRaises(ValueError):
                RunBudget(calls, tokens)

    def test_stream_deltas_do_not_count_as_requests_or_usage(self):
        chunks = [{"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]} for text in ("你", "好", "。")]
        chunks += [{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}, {"choices": [], "usage": self.usage()}]
        body = b"".join(("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks) + b"data: [DONE]\n\n"
        with patch("learn_agent.model.urllib.request.urlopen", return_value=io.BytesIO(body)):
            _, entries, _, _, _, _ = self.run_cli(call_model, args=["--stream"])
        summary = entries[-1]["summary"]
        self.assertEqual((summary["model_calls"], summary["usage"]["total_tokens"]), (1, 140))
        self.assertGreater(len([e for e in entries if e["event"] == "model_delta"]), 1)


if __name__ == "__main__":
    unittest.main()
