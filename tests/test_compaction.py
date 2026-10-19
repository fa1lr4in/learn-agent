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
from learn_agent.compaction import parse_summary
from learn_agent.context import context_size
from learn_agent.model import call_model
from learn_agent.recovery import load_recovery
from learn_agent.sessions import SessionStore
from learn_agent.tasks import replay_events


SUMMARY = {"goal": "核对实验7391", "constraints": ["不得修改文件"], "completed": ["已读取资料"],
           "pending": ["还需运行 main.py"], "evidence": ["m3：资料中部记录口令 middle-7391"], "next_step": "运行入口核验输出"}


def response(message, finish="stop", usage=None):
    return {"choices": [{"message": message, "finish_reason": finish}], **({"usage": usage} if usage is not None else {})}


class SummaryValidationTests(unittest.TestCase):
    def test_complete_structured_summary_is_accepted(self):
        self.assertEqual(parse_summary(response({"content": json.dumps(SUMMARY)})), SUMMARY)

    def test_truncated_tool_or_invalid_json_reply_is_rejected(self):
        replies = [response({"content": json.dumps(SUMMARY)}, "length"),
                   response({"content": json.dumps(SUMMARY), "tool_calls": [{"id": "forbidden"}]}),
                   response({"content": ""}), response({"content": "```json\n{}\n```"}),
                   response({"content": None}), {"choices": []}]
        for reply in replies:
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                parse_summary(reply)

    def test_missing_fields_wrong_types_and_empty_goal_are_rejected(self):
        cases = [{}, {**SUMMARY, "extra": True}, {**SUMMARY, "goal": " "}, {**SUMMARY, "next_step": None},
                 {**SUMMARY, "pending": "运行入口"}, {**SUMMARY, "evidence": [42]}]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_summary(response({"content": json.dumps(data)}))


class SemanticCompactionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-semantic-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "中文 项目"
        self.project.mkdir()
        self.content = "实验编号7391\n" + "填充记录abcdefghij\n" * 120 + "关键口令 middle-7391\n" + "填充记录abcdefghij\n" * 120 + "待办：运行 main.py"
        for i in range(6):
            (self.project / f"note-{i}.txt").write_text(self.content, encoding="utf-8")
        (self.project / "main.py").write_text("print(5050)\n", encoding="utf-8")
        self.logs = self.root / "logs"
        patcher = patch.object(tool_call, "LOG_DIR", self.logs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = SessionStore(self.logs / "sessions.sqlite3")

    def task_reply(self, index=None, name="read_file", arguments=None):
        message = {"role": "assistant", "content": "实验7391，入口输出5050。"}
        finish = "stop"
        if index is not None:
            finish = "tool_calls"
            message = {"role": "assistant", "content": "", "tool_calls": [{"id": f"call_{index}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments or {"path": f"note-{index}.txt"}),
            }}]}
        return response(message, finish, {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100})

    def summary_reply(self, data=None, finish="stop", usage=True):
        return response({"role": "assistant", "content": json.dumps(SUMMARY if data is None else data, ensure_ascii=False)}, finish,
                        {"prompt_tokens": 300, "completion_tokens": 100, "total_tokens": 400} if usage else None)

    def script(self, summaries=None, notes=3, run=True):
        tasks = iter([*[self.task_reply(i) for i in range(notes)],
                      *([self.task_reply(99, "run_python", {"path": "main.py"})] if run else []), self.task_reply()])

        def model(payload, key, **kwargs):
            if payload.get("response_format"):
                if isinstance(summaries, BaseException):
                    raise summaries
                return self.summary_reply() if summaries is None else copy.deepcopy(summaries)
            return next(tasks)
        return model

    def run_cli(self, replies=(), args=(), error=False, semantic=True):
        before = set(self.logs.glob("*.jsonl"))
        output = io.StringIO()
        argv = ["tool_call.py", "--workspace", str(self.project), "--auto-read", "--context-chars", "9000",
                *(["--semantic-compact"] if semantic else []), *args, "核对资料，保留未完成事项，不修改文件；运行入口后简要回答。"]
        with patch.object(sys, "argv", argv), patch("tool_call.get_api_key", return_value="offline-sensitive-key") as key:
            with patch("learn_agent.loop.call_model", side_effect=replies) as model, patch("learn_agent.compaction.call_model", model):
                with patch("builtins.input", return_value="y") as approval:
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                        if error:
                            with self.assertRaises(SystemExit) as caught:
                                tool_call.main()
                            output.write(str(caught.exception))
                        else:
                            tool_call.main()
        paths = set(self.logs.glob("*.jsonl")) - before
        path = next(iter(paths)) if paths else None
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path else []
        return path, entries, output.getvalue(), model, key, approval

    def test_semantic_call_uses_original_history_without_tools_and_shares_budget(self):
        _, entries, output, model, _, approval = self.run_cli(self.script())
        requests = [e for e in entries if e["event"] == "compaction_request"]
        self.assertEqual(len(requests), 1)
        payload = requests[0]["payload"]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertFalse(payload["stream"])
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        data = json.loads(payload["messages"][-1]["content"])
        self.assertIn("不修改文件", data["user_requirements"][0]["content"])
        self.assertIn("middle-7391", json.dumps(data, ensure_ascii=False))
        self.assertIsNone(data["previous_summary"])
        summary = entries[-1]["summary"]
        self.assertEqual((summary["model_calls"], summary["task_model_calls"], summary["summary_model_calls"]), (6, 5, 1))
        self.assertEqual(summary["usage"], {"prompt_tokens": 700, "completion_tokens": 200, "total_tokens": 900})
        self.assertEqual(summary["responses_received"], 6)
        self.assertEqual(model.call_count, 6)
        approval.assert_called_once()
        result = [e["result"] for e in entries if e["event"] == "tool_result"][-1]
        self.assertEqual((result["exit_code"], result["stdout"]), (0, "5050\n"))
        self.assertIn("摘要用量", output)
        self.assertEqual(entries[-1]["reason"], "stop")

    def test_cached_summary_is_reused_and_sources_and_full_storage_are_preserved(self):
        _, entries, _, _, _, _ = self.run_cli(self.script())
        ends = [e for e in entries if e["event"] == "compaction_end"]
        self.assertEqual(len(ends), 1)
        self.assertTrue(ends[0]["validated"])
        reports = [e for e in entries if e["event"] == "context_selected" and e["summary_kind"] == "model"]
        self.assertGreaterEqual(len(reports), 2)
        self.assertEqual(reports[0]["summary"], reports[-1]["summary"])
        self.assertEqual(reports[0]["summary_source_step"], ends[0]["step"])
        for entry in (e for e in entries if e["event"] == "model_request"):
            self.assertLessEqual(context_size(entry["payload"]), 9000)
        saved = self.store.list_sessions()[0]
        _, messages, runs = self.store.read(saved.id)
        self.assertEqual((len(messages), len(runs)), (10, 4))
        self.assertEqual([r.result["content"] for r in runs if r.name == "read_file"], [self.content] * 3)
        self.assertFalse(any("历史语义摘要" in (m.content or "") for m in messages))
        self.assertNotIn("offline-sensitive-key", json.dumps(entries))

    def test_expanding_summary_merges_previous_checkpoint_with_only_new_sources(self):
        _, entries, _, _, _, _ = self.run_cli(self.script(notes=6, run=False), args=["--max-model-calls", "12"])
        requests = [e for e in entries if e["event"] == "compaction_request"]
        self.assertGreaterEqual(len(requests), 2)
        previous_sources = set(requests[0]["source_indices"])
        data = json.loads(requests[1]["payload"]["messages"][-1]["content"])
        self.assertEqual(data["previous_summary"], SUMMARY)
        new_sources = {item["index"] for item in data["history"]}
        self.assertTrue(new_sources)
        self.assertFalse(new_sources & previous_sources)
        self.assertEqual(set(requests[1]["source_indices"]), previous_sources | new_sources)
        self.assertEqual(entries[-1]["reason"], "stop")

    def test_short_context_or_disabled_flag_does_not_call_summarizer(self):
        for semantic, args in ((True, ["--context-chars", "20000"]), (False, [])):
            with self.subTest(semantic=semantic):
                _, entries, _, _, _, _ = self.run_cli(self.script(notes=3, run=False), args=args, semantic=semantic)
                self.assertFalse(any(e["event"] == "compaction_request" for e in entries))
                self.assertEqual(entries[-1]["summary"]["summary_model_calls"], 0)

    def test_summary_at_token_threshold_stops_before_another_task_request(self):
        path, entries, _, model, _, approval = self.run_cli(self.script(), args=["--max-total-tokens", "700"])
        self.assertEqual((entries[-1]["reason"], replay_events(entries)[0].state), ("max_total_tokens", "paused"))
        self.assertEqual(entries[-1]["summary"]["usage"]["total_tokens"], 700)
        self.assertEqual(model.call_count, 4)
        approval.assert_not_called()
        plan = load_recovery(str(path), self.project)
        plan.require_safe()
        self.assertEqual([t["status"] for t in plan.tools], ["recorded"] * 3)

    def test_missing_summary_usage_is_unknown_and_pauses_with_token_budget(self):
        path, entries, _, model, _, approval = self.run_cli(self.script(self.summary_reply(usage=False)), args=["--max-total-tokens", "2000"])
        self.assertEqual(entries[-1]["reason"], "usage_unavailable")
        self.assertIsNone(entries[-1]["summary"]["usage"]["total_tokens"])
        self.assertEqual(entries[-1]["summary"]["reported_usage"]["total_tokens"], 300)
        self.assertEqual(model.call_count, 4)
        approval.assert_not_called()
        load_recovery(str(path), self.project).require_safe()

    def test_last_request_slot_used_by_summary_saves_complete_resumable_snapshot(self):
        path, entries, _, model, _, approval = self.run_cli(self.script(), args=["--max-model-calls", "4"])
        self.assertEqual(entries[-1]["reason"], "max_model_calls")
        self.assertEqual(entries[-2]["event"], "budget_paused")
        self.assertEqual(entries[-1]["summary"]["model_calls"], 4)
        saved = self.store.list_sessions()[0]
        _, history = self.store.restore(saved.id, self.project)
        self.assertEqual(len(history), 7)
        self.assertNotIn("NotExecuted", json.dumps(history))
        self.assertEqual([json.loads(m["content"])["content"] for m in history if m["role"] == "tool"], [self.content] * 3)
        model.assert_called()
        approval.assert_not_called()
        _, _, _, model, _, approval = self.run_cli([self.task_reply()], args=["--resume", str(path)], semantic=False)
        model.assert_called_once()
        approval.assert_not_called()

    def test_task_request_at_shared_limit_still_does_not_execute_its_tools(self):
        _, entries, _, model, _, _ = self.run_cli(self.script(), args=["--max-model-calls", "3"])
        self.assertEqual(entries[-1]["reason"], "max_model_calls")
        self.assertFalse(any(e["event"] == "compaction_request" for e in entries))
        self.assertEqual(len([e for e in entries if e["event"] == "tool_result"]), 2)
        saved = self.store.list_sessions()[0]
        self.assertIn("NotExecuted", self.store.restore(saved.id, self.project)[1][-1]["content"])
        self.assertEqual(model.call_count, 3)

    def test_truncated_invalid_or_tool_summary_fails_without_falling_back_to_excerpts(self):
        bad_tool = self.summary_reply()
        bad_tool["choices"][0]["message"]["tool_calls"] = [{"id": "forbidden", "function": {"name": "run_python", "arguments": '{"path":"main.py"}'}}]
        for summary in (self.summary_reply(finish="length"), self.summary_reply(data={}), bad_tool):
            with self.subTest(summary=summary):
                path, entries, _, model, _, approval = self.run_cli(self.script(summary), error=True)
                self.assertEqual((entries[-1]["reason"], replay_events(entries)[0].state), ("error", "failed"))
                self.assertEqual(entries[-1]["summary"]["model_calls"], 4)
                self.assertEqual(entries[-1]["summary"]["usage"]["total_tokens"], 700)
                self.assertFalse(next(e for e in entries if e["event"] == "compaction_end")["validated"])
                self.assertEqual(len([e for e in entries if e["event"] == "model_request"]), 3)
                self.assertEqual(len([e for e in entries if e["event"] == "tool_result"]), 3)
                model.assert_called()
                approval.assert_not_called()
                load_recovery(str(path), self.project).require_safe()

    def test_summary_timeout_or_cancel_counts_attempt_and_preserves_existing_results(self):
        for error, reason in ((TimeoutError("timeout"), "error"), (KeyboardInterrupt(), "cancelled")):
            with self.subTest(reason=reason):
                path, entries, _, _, _, _ = self.run_cli(self.script(error), error=True)
                self.assertEqual(entries[-1]["reason"], reason)
                summary = entries[-1]["summary"]
                self.assertEqual((summary["model_calls"], summary["responses_received"], summary["summary_model_calls"]), (4, 3, 1))
                self.assertIsNone(summary["usage"]["total_tokens"])
                self.assertEqual(summary["reported_usage"]["total_tokens"], 300)
                plan = load_recovery(str(path), self.project)
                self.assertEqual([t["status"] for t in plan.tools], ["recorded"] * 3)

    def test_oversized_valid_summary_pauses_without_truncating_its_json(self):
        huge = {**SUMMARY, "completed": ["X" * 6000]}
        _, entries, _, model, _, approval = self.run_cli(self.script(self.summary_reply(huge)))
        self.assertEqual(entries[-1]["reason"], "context_limit")
        self.assertEqual(next(e for e in entries if e["event"] == "compaction_end")["summary"]["data"], huge)
        self.assertEqual(len([e for e in entries if e["event"] == "model_request"]), 3)
        self.assertEqual(model.call_count, 4)
        approval.assert_not_called()

    def test_preview_does_not_generate_summary_or_write_storage(self):
        self.run_cli(self.script(notes=3, run=False), semantic=False)
        saved = self.store.list_sessions()[0]
        before = {p: p.read_bytes() for p in self.logs.iterdir()}
        path, _, output, model, key, approval = self.run_cli(args=["--session", saved.id, "--preview"])
        self.assertIsNone(path)
        self.assertIn("规划占位", output)
        for operation in (model, key, approval):
            operation.assert_not_called()
        self.assertEqual({p: p.read_bytes() for p in self.logs.iterdir()}, before)

    def test_failure_during_first_summary_after_restore_keeps_initial_history(self):
        self.run_cli(self.script(notes=3, run=False), semantic=False)
        saved = self.store.list_sessions()[0]
        old_history = self.store.restore(saved.id, self.project)[1]
        path, entries, _, _, _, _ = self.run_cli(self.script(TimeoutError("timeout")), args=["--session", saved.id], error=True)
        self.assertFalse(any(e["event"] == "model_request" for e in entries))
        self.assertEqual(entries[-1]["summary"]["summary_model_calls"], 1)
        _, messages, runs = self.store.read(saved.id)
        self.assertEqual([m.to_api() for m in messages][:-1], old_history)
        self.assertEqual(len(runs), 3)
        plan = load_recovery(str(path), self.project)
        plan.require_safe()
        self.assertEqual(plan.history[:-1], old_history)

    def test_first_summary_using_only_slot_is_resumable_without_task_request(self):
        self.run_cli(self.script(notes=3, run=False), semantic=False)
        saved = self.store.list_sessions()[0]
        path, entries, _, model, _, _ = self.run_cli(self.script(), args=["--session", saved.id, "--max-model-calls", "1"])
        self.assertEqual(entries[-1]["reason"], "max_model_calls")
        self.assertFalse(any(e["event"] == "model_request" for e in entries))
        self.assertEqual(entries[-1]["summary"]["task_model_calls"], 0)
        self.store.restore(saved.id, self.project)
        model.assert_called_once()
        _, entries, _, model, _, _ = self.run_cli([self.task_reply()], args=["--resume", str(path)], semantic=False)
        self.assertEqual(entries[-1]["reason"], "stop")
        model.assert_called_once()

    def test_flag_requires_context_limit_before_key_or_storage(self):
        with patch.object(sys, "argv", ["tool_call.py", "--semantic-compact", "任务"]):
            with patch("tool_call.get_api_key") as key, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                tool_call.main()
        key.assert_not_called()
        self.assertFalse(self.logs.exists())

    def test_streaming_task_and_nonstream_json_summary_use_real_transport_reader(self):
        script = self.script()
        transports = []

        def transport(request, **kwargs):
            payload = json.loads(request.data)
            reply = script(payload, "unused")
            if payload["stream"]:
                choice = reply["choices"][0]
                delta = dict(choice["message"])
                if delta.get("tool_calls"):
                    delta["tool_calls"] = [{**call, "index": i} for i, call in enumerate(delta["tool_calls"])]
                chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": choice["finish_reason"]}], "usage": reply["usage"]}]
                body = b"".join(("data: " + json.dumps(c) + "\n\n").encode() for c in chunks) + b"data: [DONE]\n\n"
            else:
                body = json.dumps(reply).encode()
            stream = io.BytesIO(body)
            transports.append(stream)
            return stream

        with patch("learn_agent.model.urllib.request.urlopen", side_effect=transport):
            _, entries, output, _, _, _ = self.run_cli(call_model, args=["--stream"])
        self.assertTrue(all(stream.closed for stream in transports))
        self.assertEqual(len(transports), 6)
        self.assertGreater(len([e for e in entries if e["event"] == "model_delta"]), 0)
        self.assertEqual(len([e for e in entries if e["event"] == "compaction_response"]), 1)
        self.assertEqual(entries[-1]["summary"]["usage"]["total_tokens"], 900)
        self.assertIn("第 4 轮任务回复；总第 5 次模型请求", output)
        self.assertEqual(entries[-1]["reason"], "stop")

    def test_incomplete_budget_snapshot_is_rejected_before_network(self):
        _, entries, _, _, _, _ = self.run_cli(self.script(), args=["--max-model-calls", "4"])
        entries[-2]["full_messages"].pop()
        source = self.root / "invalid-snapshot.jsonl"
        source.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        path, _, _, model, key, approval = self.run_cli(args=["--resume", str(source)], error=True)
        self.assertIsNone(path)
        for operation in (model, key, approval):
            operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
