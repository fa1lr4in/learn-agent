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
from learn_agent.context import ContextLimitError, SUMMARY_HEADER, context_size, select_context
from learn_agent.events import validate_history
from learn_agent.model import call_model
from learn_agent.recovery import load_recovery
from learn_agent.sessions import SessionStore
from learn_agent.tasks import replay_events


def tool_call_message(call_id, name="read_file", arguments=None):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {"path": "notes.txt"}),
    }}]}


class ContextSelectionTests(unittest.TestCase):
    def payload(self, count=8):
        messages = [{"role": "system", "content": "系统规则"}, {"role": "user", "content": "原始要求：不得修改文件，核对实验编号和未完成事项。"}]
        for index in range(count):
            messages += [tool_call_message(f"call_{index}"), {"role": "tool", "tool_call_id": f"call_{index}",
                "content": json.dumps({"ok": True, "tool": "read_file", "content": f"记录 {index}：" + "材料" * 250}, ensure_ascii=False)}]
        messages.append({"role": "user", "content": "新增要求：需要重新确认当前状态。"})
        return {"messages": messages, "tools": [{"type": "function", "function": {"name": "read_file"}}]}

    def test_small_context_is_unchanged_and_counts_tool_schema(self):
        payload = self.payload(1)
        before = copy.deepcopy(payload)
        selected, report = select_context(payload, 20000)
        self.assertEqual(payload, before)
        self.assertEqual(selected, payload)
        self.assertEqual(report["omitted_indices"], [])
        self.assertEqual(report["shortened_tools"], [])
        self.assertEqual(report["selected_chars"], len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))

    def test_older_history_is_replaced_with_bounded_traceable_excerpts(self):
        payload = self.payload()
        original = copy.deepcopy(payload)
        selected, report = select_context(payload, 2600)
        self.assertLessEqual(context_size(selected), 2600)
        self.assertTrue(report["omitted_indices"])
        self.assertTrue(report["summary"].startswith(SUMMARY_HEADER))
        self.assertIn("m2", report["summary"])
        self.assertIn("可能遗漏细节", report["summary"])
        self.assertEqual(payload, original)
        self.assertEqual([m for m in selected["messages"] if m["role"] == "user"], [m for m in payload["messages"] if m["role"] == "user"])
        self.assertEqual(selected["messages"][-3:], payload["messages"][-3:])
        validate_history(selected["messages"][1:])

    def test_multi_tool_batch_is_never_split_by_history_selection(self):
        payload = self.payload()
        payload["messages"][2]["tool_calls"].append({"id": "second", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"other.txt"}'}})
        payload["messages"].insert(4, {"role": "tool", "tool_call_id": "second", "content": "第二个结果"})
        selected, report = select_context(payload, 2600)
        batch = {2, 3, 4}
        self.assertIn(set(report["omitted_indices"]) & batch, (set(), batch))
        validate_history(selected["messages"][1:])

    def test_long_tool_projection_is_valid_json_and_retains_status_and_source(self):
        payload = self.payload(1)
        result = {"ok": False, "tool": "run_command", "path": ".", "exit_code": 1, "timed_out": False,
                  "cleanup_succeeded": True, "stderr_truncated": False, "error": {"type": "ExampleError", "message": "失败"},
                  "stdout": "开头标记" + "内容" * 3000 + "末尾标记"}
        payload["messages"][3]["content"] = json.dumps(result, ensure_ascii=False)
        before = copy.deepcopy(payload)
        selected, report = select_context(payload, 20000)
        view = json.loads(selected["messages"][3]["content"])
        self.assertTrue(view["context_truncated"])
        self.assertEqual(view["source_message"], 3)
        self.assertEqual(view["metadata"]["exit_code"], 1)
        self.assertFalse(view["metadata"]["ok"])
        self.assertIn("ExampleError", view["metadata"]["error_excerpt"])
        self.assertIn("开头标记", view["excerpt"])
        self.assertIn("末尾标记", view["excerpt"])
        self.assertEqual(report["shortened_tools"], [3])
        self.assertEqual(payload, before)

    def test_plain_text_tool_result_is_marked_instead_of_parsed_as_complete_json(self):
        payload = self.payload(1)
        payload["messages"][3]["content"] = "普通文本" * 1000
        selected, _ = select_context(payload, 20000)
        self.assertTrue(json.loads(selected["messages"][3]["content"])["context_truncated"])

    def test_system_users_and_latest_batch_are_not_silently_cut(self):
        payload = self.payload(1)
        payload["messages"][0]["content"] = "系统规则" * 1000
        before = copy.deepcopy(payload)
        with self.assertRaises(ContextLimitError) as error:
            select_context(payload, 100)
        self.assertTrue(error.exception.report["blocked"])
        self.assertGreater(error.exception.report["selected_chars"], 100)
        self.assertEqual(payload, before)

    def test_incomplete_tool_pair_and_invalid_limit_are_rejected(self):
        payload = self.payload(1)
        payload["messages"].pop(3)
        with self.assertRaises(ValueError):
            select_context(payload, 20000)
        for limit in (0, -1, True, "2000"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                select_context(self.payload(1), limit)


class ContextIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-context-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "中文 项目"
        self.project.mkdir()
        self.content = "实验编号：7391\n" + "历史材料abcdefghij\n" * 300 + "\n未完成事项：核对入口输出。"
        for index in range(6):
            (self.project / f"notes{index}.txt").write_text(self.content, encoding="utf-8")
        (self.project / "main.py").write_text("print(5050)\n", encoding="utf-8")
        self.logs = self.root / "logs"
        patcher = patch.object(tool_call, "LOG_DIR", self.logs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = SessionStore(self.logs / "sessions.sqlite3")

    def reply(self, index=None, name="read_file", arguments=None):
        message = {"role": "assistant", "content": "已核对，编号7391，未完成事项是核对入口输出。"} if index is None else tool_call_message(
            f"call_{index}", name, arguments or {"path": f"notes{index}.txt"})
        return {"choices": [{"message": message, "finish_reason": "stop" if index is None else "tool_calls"}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}}

    def run_cli(self, replies=(), args=(), answer="y", error=False):
        before = set(self.logs.glob("*.jsonl"))
        output = io.StringIO()
        with patch.object(sys, "argv", ["tool_call.py", "--workspace", str(self.project), *args, "只读核对实验编号，保留未完成事项，不修改文件。"]):
            with patch("tool_call.get_api_key", return_value="offline-private-context-key") as key:
                with patch("learn_agent.providers.call_model", side_effect=replies) as model, patch("builtins.input", return_value=answer) as approval:
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

    def long_run(self, tail=None):
        return self.run_cli([*[self.reply(i) for i in range(6)], self.reply() if tail is None else tail],
                            args=["--auto-read", "--context-chars", "9000"], error=isinstance(tail, BaseException))

    def test_long_task_continues_with_bounded_requests_and_full_sqlite_history(self):
        _, entries, _, model, _, approval = self.long_run()
        reports = [e for e in entries if e["event"] == "context_selected"]
        self.assertTrue(any(e["omitted_indices"] for e in reports))
        self.assertTrue(any(e["shortened_tools"] for e in reports))
        self.assertEqual(len(reports), model.call_count)
        approval.assert_not_called()
        for request in model.call_args_list:
            payload = request.args[0]
            self.assertLessEqual(context_size(payload), 9000)
            self.assertEqual(payload["messages"][1]["content"], "只读核对实验编号，保留未完成事项，不修改文件。")
            validate_history(payload["messages"][1:])
        saved = self.store.list_sessions()[0]
        _, messages, runs = self.store.read(saved.id)
        self.assertEqual((len(messages), len(runs)), (14, 6))
        for message in messages:
            if message.role == "tool":
                self.assertEqual(json.loads(message.content)["content"], self.content)
        self.assertTrue(all(run.result["content"] == self.content for run in runs))
        self.assertFalse(any(SUMMARY_HEADER in (message.content or "") for message in messages))
        self.assertEqual(entries[-1]["reason"], "stop")
        self.assertEqual(entries[-1]["summary"]["usage"]["total_tokens"], 700)
        self.assertNotIn("offline-private-context-key", json.dumps(entries))

    def test_raw_and_selected_messages_are_independently_traceable(self):
        _, entries, _, _, _, _ = self.long_run()
        reports = {e["step"]: e for e in entries if e["event"] == "context_selected"}
        for entry in (e for e in entries if e["event"] == "model_request"):
            full = entry["full_messages"]
            report = reports[entry["step"]]
            self.assertEqual(report["original_messages"], len(full))
            self.assertEqual(report["original_chars"], context_size({**entry["payload"], "messages": full}))
            self.assertEqual(report["selected_chars"], context_size(entry["payload"]))
            for index in report["shortened_tools"]:
                raw = full[index]
                view = next(m for m in entry["payload"]["messages"] if m.get("tool_call_id") == raw["tool_call_id"])
                self.assertEqual(json.loads(view["content"])["source_message"], index)
                self.assertEqual(json.loads(raw["content"])["content"], self.content)

    def test_session_preview_is_read_only_and_shows_summary(self):
        self.long_run()
        saved = self.store.list_sessions()[0]
        before = {p: p.read_bytes() for p in self.logs.iterdir()}
        path, _, output, model, key, approval = self.run_cli(args=["--session", saved.id, "--context-chars", "9000", "--preview"])
        self.assertIsNone(path)
        self.assertIn("历史摘录", output)
        self.assertIn("上下文选择", output)
        for operation in (model, key, approval):
            operation.assert_not_called()
        self.assertEqual(before, {p: p.read_bytes() for p in self.logs.iterdir()})

    def test_session_without_flag_uses_full_history_and_keeps_same_id(self):
        self.long_run()
        saved = self.store.list_sessions()[0]
        _, entries, _, model, _, approval = self.run_cli([self.reply()], args=["--session", saved.id])
        request = model.call_args.args[0]
        self.assertEqual(len(request["messages"]), 16)
        self.assertTrue(all(json.loads(m["content"])["content"] == self.content for m in request["messages"] if m["role"] == "tool"))
        self.assertFalse(any(e["event"] == "context_selected" for e in entries))
        self.assertIsNone(entries[0]["context_chars"])
        self.assertEqual(entries[0]["session_id"], saved.id)
        approval.assert_not_called()
        self.assertEqual(len(self.store.read(saved.id)[2]), 6)

    def test_jsonl_resume_restores_full_original_results(self):
        path, entries, _, _, _, _ = self.long_run()
        original = path.read_bytes()
        _, _, _, model, _, approval = self.run_cli([self.reply()], args=["--resume", str(path)])
        request = model.call_args.args[0]
        self.assertTrue(all(json.loads(m["content"])["content"] == self.content for m in request["messages"] if m["role"] == "tool"))
        self.assertEqual(path.read_bytes(), original)
        approval.assert_not_called()

    def test_cancelled_compressed_request_recovers_raw_history_without_replay(self):
        path, entries, _, _, _, _ = self.long_run(KeyboardInterrupt())
        plan = load_recovery(str(path), self.project)
        plan.require_safe()
        self.assertTrue(all(t["status"] == "recorded" for t in plan.tools))
        self.assertTrue(all(json.loads(m["content"])["content"] == self.content for m in plan.history if m["role"] == "tool"))
        self.assertEqual(plan.history, next(e for e in reversed(entries) if e["event"] == "model_request")["full_messages"][1:])
        _, _, _, model, _, approval = self.run_cli([self.reply()], args=["--recover", str(path), "--context-chars", "9000"])
        approval.assert_not_called()
        model.assert_called_once()

    def test_request_limit_repairs_only_unexecuted_calls_and_restores_raw_results(self):
        path, entries, _, _, _, _ = self.run_cli([self.reply(i) for i in range(3)], args=["--auto-read", "--context-chars", "9000", "--max-model-calls", "3"])
        self.assertEqual(entries[-1]["reason"], "max_model_calls")
        saved = self.store.list_sessions()[0]
        _, history = self.store.restore(saved.id, self.project)
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        self.assertEqual([r["content"] for r in results[:-1]], [self.content, self.content])
        self.assertEqual(results[-1]["error"]["type"], "NotExecuted")
        _, _, _, model, _, approval = self.run_cli([self.reply()], args=["--resume", str(path)])
        self.assertIn("NotExecuted", json.dumps(model.call_args.args[0]))
        approval.assert_not_called()

    def test_context_limit_after_write_keeps_side_effect_and_can_recover(self):
        content = "实际已写入\n" + "X" * 10000
        path, entries, output, model, _, approval = self.run_cli([
            self.reply(0, "write_file", {"path": "result.txt", "content": content}),
        ], args=["--context-chars", "7000"])
        self.assertEqual((entries[-1]["reason"], replay_events(entries)[0].state), ("context_limit", "paused"))
        self.assertEqual(entries[-1]["summary"]["model_calls"], 1)
        self.assertEqual((self.project / "result.txt").read_text(encoding="utf-8"), content)
        self.assertTrue(next(e for e in reversed(entries) if e["event"] == "context_selected")["blocked"])
        self.assertIn("未请求本轮模型", output)
        model.assert_called_once()
        approval.assert_called_once()
        plan = load_recovery(str(path), self.project)
        plan.require_safe()
        self.assertEqual(plan.tools[0]["status"], "recorded")
        _, _, _, model, _, approval = self.run_cli([self.reply()], args=["--recover", str(path), "--context-chars", "25000"])
        approval.assert_not_called()
        model.assert_called_once()
        self.assertEqual((self.project / "result.txt").read_text(encoding="utf-8"), content)

    def test_first_request_too_large_and_invalid_flags_fail_before_credentials(self):
        for value in ("1", "0", "-1", "abc"):
            with self.subTest(value=value):
                path, _, _, model, key, approval = self.run_cli(args=["--context-chars", value], error=True)
                self.assertIsNone(path)
                for operation in (model, key, approval):
                    operation.assert_not_called()
        self.assertFalse(self.logs.exists())

    def test_disabled_selection_preserves_existing_full_request(self):
        _, entries, _, model, _, _ = self.run_cli([self.reply(0), self.reply()], args=["--auto-read"])
        result = next(m for m in model.call_args.args[0]["messages"] if m["role"] == "tool")
        self.assertEqual(json.loads(result["content"])["content"], self.content)
        self.assertFalse(any("full_messages" in e for e in entries))
        self.assertFalse(any(e["event"] == "context_selected" for e in entries))

    def test_streaming_uses_selected_request_and_keeps_original_tool_run(self):
        requests = []

        def transport(request, **kwargs):
            payload = json.loads(request.data)
            requests.append(payload)
            response = self.reply(0) if len(requests) == 1 else self.reply()
            choice = response["choices"][0]
            delta = dict(choice["message"])
            if delta.get("tool_calls"):
                delta["tool_calls"] = [{**call, "index": i} for i, call in enumerate(delta["tool_calls"])]
            chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": choice["finish_reason"]}], "usage": response["usage"]}]
            return io.BytesIO(b"".join(("data: " + json.dumps(c) + "\n\n").encode() for c in chunks) + b"data: [DONE]\n\n")

        with patch("learn_agent.model.urllib.request.urlopen", side_effect=transport):
            _, entries, _, _, _, _ = self.run_cli(call_model, args=["--auto-read", "--stream", "--context-chars", "9000"])
        self.assertEqual(len(requests), 2)
        self.assertTrue(json.loads(next(m for m in requests[-1]["messages"] if m["role"] == "tool")["content"])["context_truncated"])
        saved = self.store.list_sessions()[0]
        self.assertEqual(self.store.read(saved.id)[2][0].result["content"], self.content)
        self.assertEqual(entries[-1]["reason"], "stop")


if __name__ == "__main__":
    unittest.main()
