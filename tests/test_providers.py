import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import tool_call
from learn_agent.budget import RunBudget
from learn_agent.events import history_from_entries
from learn_agent.providers import DeepSeekProvider, ModelResponse
from learn_agent.sessions import SessionStore


class AlternateProvider:
    """Offline service with a different response and JSON-mode convention."""

    name = "offline-alternate"

    def __init__(self, replies, summary=None):
        self.replies = iter(replies)
        self.summary = summary
        self.requests = []

    def build_request(self, messages, *, tools=None, stream=False, max_tokens=2048, json_mode=False, model=None):
        payload = {"model": model or "offline-model", "messages": messages, "stream": stream,
                   "max_tokens": max_tokens, "output_json": json_mode}
        if tools is not None:
            payload["tools"] = tools
        return payload

    def complete(self, payload, api_key, on_delta=None):
        self.requests.append(copy.deepcopy(payload))
        raw = self.summary if payload.get("output_json") else next(self.replies)
        message = {"role": "assistant", "content": raw["answer"]}
        if raw.get("actions"):
            message["tool_calls"] = raw["actions"]
        if on_delta is not None:
            on_delta({"content": raw["answer"]})
        return ModelResponse(message, {"finished": "stop", "action": "tool_calls", "limit": "length"}[raw["end"]],
                             {"prompt_tokens": raw["tokens"]["in"], "completion_tokens": raw["tokens"]["out"],
                              "total_tokens": raw["tokens"]["in"] + raw["tokens"]["out"]})


def alternate_reply(actions=None, answer="已完成。", end=None):
    return {"answer": answer, "actions": actions, "end": end or ("action" if actions else "finished"),
            "tokens": {"in": 80, "out": 20}}


def make_call(name, arguments, call_id="call-1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class DeepSeekProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = DeepSeekProvider()
        self.messages = [{"role": "user", "content": "你好"}]

    def test_chat_defaults_environment_and_explicit_model(self):
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": "configured-model", "DEEPSEEK_API_KEY": "private-placeholder"}):
            payload = self.provider.build_request(self.messages, max_tokens=512)
            explicit = self.provider.build_request(self.messages, model="explicit-model")
        self.assertEqual(payload, {"model": "configured-model", "messages": self.messages, "stream": False,
                                   "thinking": {"type": "disabled"}, "max_tokens": 512})
        self.assertEqual(explicit["model"], "explicit-model")
        self.assertNotIn("private-placeholder", json.dumps(payload))

    def test_tool_stream_and_summary_requests_keep_distinct_options(self):
        task = self.provider.build_request(self.messages, tools=[{"function": {"name": "read_file"}}], stream=True)
        summary = self.provider.build_request(self.messages, model=task["model"], json_mode=True, max_tokens=1024)
        self.assertTrue(task["stream"])
        self.assertEqual(task["tool_choice"], "auto")
        self.assertFalse(summary["stream"])
        self.assertEqual(summary["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", summary)
        self.assertNotIn("tool_choice", summary)
        self.assertEqual(summary["model"], task["model"])

    def test_json_transport_returns_common_message_reason_usage_and_metadata(self):
        raw = {"id": "reply-1", "model": "configured-model", "choices": [{"message": {
            "role": "assistant", "content": "你好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}
        stream = io.BytesIO(json.dumps(raw).encode())
        with patch("learn_agent.model.urllib.request.urlopen", return_value=stream) as transport:
            result = self.provider.complete(self.provider.build_request(self.messages), "private-placeholder")
        self.assertEqual(result.message["content"], "你好")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.usage["total_tokens"], 10)
        self.assertEqual(result.metadata["id"], "reply-1")
        self.assertEqual(result.as_dict(), raw)
        self.assertTrue(stream.closed)
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertNotIn("private-placeholder", json.dumps(result.as_dict()))

    def test_missing_or_invalid_usage_remains_unknown(self):
        for usage in (None, "unavailable", {}):
            raw = {"choices": [{"message": {"content": "完成"}, "finish_reason": "stop"}], "usage": usage}
            result = self.provider.parse_response(raw)
            budget = RunBudget(3, 100)
            with contextlib.redirect_stdout(io.StringIO()):
                budget.start_call()
                budget.observe(result.as_dict())
            self.assertEqual(budget.pause_reason(), "usage_unavailable")

    def test_length_and_tool_calls_are_preserved_for_core_decisions(self):
        call = make_call("read_file", {"path": "notes.txt"})
        for reason, message in (("length", {"content": "截断"}),
                                ("tool_calls", {"content": None, "tool_calls": [call]})):
            result = self.provider.parse_response({"choices": [{"message": message, "finish_reason": reason}]})
            self.assertEqual(result.finish_reason, reason)
            self.assertEqual(result.message, message)

    def test_malformed_json_response_raises_a_common_error(self):
        valid = {"message": {"content": "完成"}, "finish_reason": "stop"}
        for raw in (None, {}, {"choices": []}, {"choices": [valid, valid]}, {"choices": [{"message": {}}]},
                    {"choices": [{**valid, "message": {"content": 0}}]},
                    {"choices": [{**valid, "message": {"role": "tool"}}]}):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "DeepSeek 回复格式无效"):
                self.provider.parse_response(raw)

    def test_http_error_is_not_retried_or_converted_to_success(self):
        error = urllib.error.HTTPError("https://api.deepseek.com/chat/completions", 429, "offline", {}, None)
        with patch("learn_agent.model.urllib.request.urlopen", side_effect=error) as transport:
            with self.assertRaises(urllib.error.HTTPError):
                self.provider.complete(self.provider.build_request(self.messages), "private-placeholder")
        transport.assert_called_once()

    def test_sse_fragments_reach_callback_and_only_complete_tools_are_returned(self):
        fragments = [
            {"content": "准备读取", "tool_calls": [{"index": 0, "id": "call-stream", "type": "function",
              "function": {"name": "read_file", "arguments": '{"path":'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '"notes.txt"}'}}]},
        ]
        chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in fragments]
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                       "usage": {"total_tokens": 12}})
        wire = b"".join(("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks) + b"data: [DONE]\n\n"
        stream, deltas = io.BytesIO(wire), []
        with patch("learn_agent.model.urllib.request.urlopen", return_value=stream):
            result = self.provider.complete(self.provider.build_request(self.messages, stream=True), "offline", deltas.append)
        self.assertEqual(result.message["tool_calls"][0]["function"]["arguments"], '{"path":"notes.txt"}')
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(result.usage["total_tokens"], 12)
        self.assertEqual(len(deltas), 2)
        self.assertTrue(stream.closed)


class ProviderLoopTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-provider-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "中文 项目"
        self.project.mkdir()
        (self.project / "notes.txt").write_text("项目小灯，编号7391", encoding="utf-8")
        (self.project / "main.py").write_text("print(5050)\n", encoding="utf-8")

    def run_cli(self, provider, extra=(), answer="y"):
        logs = self.root / "logs"
        argv = ["tool_call.py", "--workspace", str(self.project), *extra, "核对资料，按要求运行入口后简要回答。"]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(tool_call, "LOG_DIR", logs):
            with patch("tool_call.DeepSeekProvider", return_value=provider), patch("tool_call.get_api_key", return_value="offline"):
                with patch("builtins.input", return_value=answer) as approval, contextlib.redirect_stdout(output):
                    with patch("learn_agent.providers.call_model") as deepseek:
                        tool_call.main()
        entries = [json.loads(line) for line in next(logs.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        deepseek.assert_not_called()
        return entries, output.getvalue(), approval, logs

    def test_alternate_response_drives_tools_approval_budget_and_restorable_history(self):
        provider = AlternateProvider([alternate_reply([make_call("read_file", {"path": "notes.txt"})]),
                                      alternate_reply([make_call("run_python", {"path": "main.py"}, "run-1")]),
                                      alternate_reply(answer="实验7391，输出5050。")])
        entries, output, approval, logs = self.run_cli(provider, ["--auto-read", "--stream"])
        self.assertEqual(entries[0]["provider"], provider.name)
        self.assertEqual(entries[-1]["task_state"], "completed")
        self.assertEqual(entries[-1]["summary"]["usage"]["total_tokens"], 300)
        approval.assert_called_once()
        result = next(e["result"] for e in entries if e["event"] == "tool_result" and e["tool"] == "run_python")
        self.assertEqual(result["stdout"], "5050\n")
        self.assertIn("model_delta", {e["event"] for e in entries})
        self.assertTrue(all("thinking" not in p for p in provider.requests))
        history = history_from_entries(entries, self.project)
        self.assertEqual(history[-1]["content"], "实验7391，输出5050。")
        saved, restored = SessionStore(logs / "sessions.sqlite3").restore(entries[0]["session_id"], self.project)
        self.assertEqual(restored, history)

    def test_alternate_usage_still_pauses_before_executing_tools(self):
        provider = AlternateProvider([alternate_reply([make_call("run_python", {"path": "main.py"})])])
        entries, _, approval, _ = self.run_cli(provider, ["--max-total-tokens", "1"])
        self.assertEqual(entries[-1]["reason"], "max_total_tokens")
        self.assertFalse(any(e["event"] == "tool_result" for e in entries))
        approval.assert_not_called()

    def test_alternate_length_reply_never_executes_pending_tools(self):
        provider = AlternateProvider([alternate_reply([make_call("run_python", {"path": "main.py"})], end="limit")])
        entries, _, approval, _ = self.run_cli(provider)
        self.assertEqual(entries[-1]["reason"], "abnormal_finish")
        self.assertFalse(any(e["event"] == "tool_started" for e in entries))
        approval.assert_not_called()

    def test_same_provider_generates_summary_and_both_calls_share_budget(self):
        content = "实验编号7391\n" + "填充记录abcdefghij\n" * 120 + "关键口令 middle-7391\n" + "填充记录abcdefghij\n" * 120 + "待办：运行 main.py"
        for i in range(3):
            (self.project / f"note-{i}.txt").write_text(content, encoding="utf-8")
        summary = {"goal": "核对资料", "constraints": [], "completed": ["已读取资料"], "pending": ["运行入口"],
                   "evidence": ["7391"], "next_step": "运行 main.py"}
        provider = AlternateProvider([*[alternate_reply([make_call("read_file", {"path": f"note-{i}.txt"}, f"read-{i}")]) for i in range(3)],
                                      alternate_reply([make_call("run_python", {"path": "main.py"}, "run-1")]),
                                      alternate_reply()], alternate_reply(answer=json.dumps(summary)))
        entries, _, _, _ = self.run_cli(provider, ["--auto-read", "--stream", "--context-chars", "9000", "--semantic-compact"])
        self.assertEqual(entries[-1]["reason"], "stop")
        self.assertEqual(entries[-1]["summary"]["task_model_calls"], 5)
        self.assertEqual(entries[-1]["summary"]["summary_model_calls"], 1)
        self.assertEqual(entries[-1]["summary"]["usage"]["total_tokens"], 600)
        request = next(p for p in provider.requests if p["output_json"])
        self.assertFalse(request["stream"])
        self.assertNotIn("tools", request)
        self.assertNotIn("response_format", request)
        self.assertTrue(any(e["event"] == "compaction_end" and e["validated"] for e in entries))


if __name__ == "__main__":
    unittest.main()
