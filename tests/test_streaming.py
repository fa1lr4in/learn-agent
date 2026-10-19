import contextlib
import http.client
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call
from learn_agent import tools
from learn_agent.model import call_model
from learn_agent.recovery import load_recovery
from learn_agent.sessions import SessionStore
from learn_agent.streaming import iter_sse_data, read_stream
from learn_agent.tasks import replay_events


class StreamResponse:
    def __init__(self, lines):
        self.lines = lines
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def __iter__(self):
        yield from self.lines


class StreamingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-stream-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "中文 项目"
        self.project.mkdir()
        (self.project / "main.py").write_text("print(5050)\n", encoding="utf-8")
        (self.project / "notes.txt").write_text("项目：小灯，编号：7391", encoding="utf-8")
        self.logs = self.root / "logs"
        patcher = patch.object(tool_call, "LOG_DIR", self.logs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def chunk(self, delta=None, finish=None, **details):
        return {"id": "stream-1", "model": "offline-model", "created": 123,
                "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **details}

    def event(self, chunk):
        return ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8").splitlines(keepends=True)

    def lines(self, chunks, done=True):
        yield b": keepalive\r\n\r\n"
        for chunk in chunks:
            yield from self.event(chunk)
        if done:
            yield b"data: [DONE]\n\n"

    def tool(self, index=0, name="run_python", arguments='{"path":"main.py"}', call_id="call_0"):
        return {"index": index, "id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}

    def response(self, chunks, done=True):
        return StreamResponse(self.lines(chunks, done))

    def run_cli(self, responses=(), args=(), answer="y", error=False, stream=True, output=None):
        output = output if output is not None else io.StringIO()
        before = set(self.logs.glob("*.jsonl"))
        argv = ["tool_call.py", "--workspace", str(self.project), *(["--stream"] if stream else []), *args, "验证输出"]
        with patch.object(sys, "argv", argv), patch("tool_call.get_api_key", return_value="offline-sensitive-stream-key") as key:
            with patch("learn_agent.model.urllib.request.urlopen", side_effect=responses) as transport:
                with patch("builtins.input", side_effect=answer if callable(answer) else None,
                           return_value=None if callable(answer) else answer) as approval:
                    with contextlib.redirect_stdout(output):
                        if error:
                            with self.assertRaises(SystemExit):
                                tool_call.main()
                        else:
                            tool_call.main()
        paths = set(self.logs.glob("*.jsonl")) - before
        path = next(iter(paths)) if paths else None
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path else []
        return path, entries, output.getvalue(), transport, key, approval

    def test_sse_comments_crlf_multiline_data_and_final_marker_at_eof(self):
        data = [b": ping\r\n", b"event: message\r\n", b'data: {"a":\r\n', b"data: 1}\r\n", b"\r\n", b"data: [DONE]"]
        self.assertEqual(list(iter_sse_data(data)), ['{"a":\n1}', "[DONE]"])

    def test_text_metadata_and_final_usage_are_assembled(self):
        chunks = [self.chunk({"role": "assistant", "content": ""}), self.chunk({"content": "你好，"}),
                  self.chunk({"content": "世界。"}), self.chunk({"role": None}, "stop", usage={"total_tokens": 12})]
        deltas = []
        response = read_stream(self.lines(chunks), deltas.append)
        self.assertEqual(response["choices"][0]["message"], {"role": "assistant", "content": "你好，世界。"})
        self.assertEqual(response["choices"][0]["finish_reason"], "stop")
        self.assertEqual(response["usage"], {"total_tokens": 12})
        self.assertEqual((response["id"], response["model"], response["object"]), ("stream-1", "offline-model", "chat.completion"))
        self.assertEqual(len(deltas), 4)

    def test_usage_only_chunk_preserves_existing_finish_reason(self):
        chunks = [self.chunk({"content": "完成"}, "stop"), {"choices": [], "usage": {"total_tokens": 5}}]
        result = read_stream(self.lines(chunks))
        self.assertEqual(result["usage"]["total_tokens"], 5)
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")

    def test_interleaved_tool_fragments_are_grouped_and_sorted_by_index(self):
        chunks = [self.chunk({"tool_calls": [self.tool(1, "read_file", '{"path":"notes', "call_1")]}),
                  self.chunk({"tool_calls": [self.tool(0, "read_", '{"path":"main', "call_0")]}),
                  self.chunk({"tool_calls": [{"index": 1, "function": {"arguments": '.txt"}'}}]}),
                  self.chunk({"tool_calls": [{"index": 0, "function": {"name": "file", "arguments": '.py"}'}}]}),
                  self.chunk(finish="tool_calls")]
        calls = read_stream(self.lines(chunks))["choices"][0]["message"]["tool_calls"]
        self.assertEqual([call["id"] for call in calls], ["call_0", "call_1"])
        self.assertEqual([call["function"]["name"] for call in calls], ["read_file", "read_file"])
        self.assertEqual([json.loads(call["function"]["arguments"])["path"] for call in calls], ["main.py", "notes.txt"])

    def test_missing_done_or_finish_signal_is_rejected(self):
        for chunks, done in [([self.chunk({"content": "一半"})], True),
                             ([self.chunk({"content": "已输出"}, "stop")], False), ([], True)]:
            with self.subTest(chunks=chunks, done=done), self.assertRaises(ValueError):
                read_stream(self.lines(chunks, done))

    def test_malformed_chunks_and_inconsistent_tool_identity_are_rejected(self):
        cases = [[{"choices": "invalid"}], [self.chunk({"content": 12})],
                 [self.chunk({"role": "system"})], [self.chunk({"tool_calls": [{"index": True}]})],
                 [self.chunk({"tool_calls": [{"index": -1}]})], [self.chunk({"tool_calls": [{"index": 0, "function": "bad"}]})],
                 [self.chunk({"tool_calls": [self.tool()]}), self.chunk({"tool_calls": [{"index": 0, "id": "another"}]})],
                 [self.chunk({"content": "结束"}, "stop"), self.chunk({"content": "又新增"})],
                 [self.chunk(finish="stop"), self.chunk(finish="length")],
                 [self.chunk({"tool_calls": [{"index": 0, "function": {"arguments": 1}}]})],
                 [self.chunk({"tool_calls": [self.tool()]}, "stop")],
                 [self.chunk(finish="tool_calls")],
                 [self.chunk({"tool_calls": [{"index": 0, "function": {"name": "run_python", "arguments": "{}"}}]}, "tool_calls")]]
        for chunks in cases:
            with self.subTest(chunks=chunks), self.assertRaises(ValueError):
                read_stream(self.lines(chunks))
        with self.assertRaises(ValueError):
            read_stream([b"data: {invalid}\n\n"])

    def test_preview_enables_stream_without_key_network_or_storage(self):
        path, _, output, transport, key, approval = self.run_cli(args=["--preview"])
        payload, _ = json.JSONDecoder().raw_decode(output.split("\n", 1)[1])
        self.assertTrue(payload["stream"])
        self.assertIsNone(path)
        self.assertFalse(self.logs.exists())
        for operation in (transport, key, approval):
            operation.assert_not_called()

    def test_text_is_printed_before_next_fragment_and_not_repeated(self):
        output = io.StringIO()
        def frames():
            yield from self.event(self.chunk({"content": "实时"}))
            self.assertIn("实时", output.getvalue())
            self.assertNotIn("回答完成", output.getvalue())
            yield from self.event(self.chunk({"content": "回答完成"}))
            self.assertIn("实时回答完成", output.getvalue())
            yield from self.event(self.chunk(finish="stop", usage={"total_tokens": 9}))
            yield b"data: [DONE]\n\n"
        response = StreamResponse(frames())
        path, entries, text, transport, _, approval = self.run_cli([response], output=output)
        self.assertTrue(response.closed)
        self.assertEqual(text.count("实时回答完成"), 1)
        approval.assert_not_called()
        transport.assert_called_once()
        request = json.loads(transport.call_args.args[0].data)
        self.assertTrue(request["stream"])
        self.assertEqual(sum(entry["event"] == "model_delta" for entry in entries), 2)
        final = next(entry for entry in entries if entry["event"] == "model_response")
        self.assertEqual(final["response"]["usage"]["total_tokens"], 9)
        self.assertEqual((entries[-1]["reason"], replay_events(entries)[0].state), ("stop", "completed"))
        store = SessionStore(self.logs / "sessions.sqlite3")
        session = store.list_sessions()[0]
        self.assertEqual(store.read(session.id)[1][-1].content, "实时回答完成")
        self.assertNotIn("offline-sensitive-stream-key", path.read_text(encoding="utf-8"))

    def test_tool_is_not_approved_or_run_until_done_and_then_runs_once(self):
        output = io.StringIO()
        reached_done = False
        def frames():
            nonlocal reached_done
            yield from self.event(self.chunk({"tool_calls": [self.tool(arguments='{"path":"main')]}))
            runner.assert_not_called()
            yield from self.event(self.chunk({"tool_calls": [{"index": 0, "function": {"arguments": '.py"}'}}]}))
            runner.assert_not_called()
            self.assertNotIn("输入 y", output.getvalue())
            yield from self.event(self.chunk(finish="tool_calls"))
            runner.assert_not_called()
            reached_done = True
            yield b"data: [DONE]\n\n"
        first = StreamResponse(frames())
        def approve(prompt):
            self.assertTrue(reached_done)
            self.assertTrue(first.closed)
            return "y"
        with patch("learn_agent.tools.run_python", wraps=tools.run_python) as runner:
            _, entries, text, transport, _, approval = self.run_cli([
                first, self.response([self.chunk({"content": "输出为 5050。"}, "stop")]),
            ], answer=approve, output=output)
        runner.assert_called_once_with("main.py")
        approval.assert_called_once()
        self.assertEqual(transport.call_count, 2)
        result = next(entry["result"] for entry in entries if entry["event"] == "tool_result")
        self.assertEqual((result["ok"], result["exit_code"], result["stdout"]), (True, 0, "5050\n"))
        events = [entry["event"] for entry in entries]
        self.assertLess(events.index("model_response"), events.index("tool_waiting"))
        self.assertLess(events.index("tool_approval"), events.index("tool_started"))
        self.assertIn("[完整工具请求]", text)

    def test_interleaved_read_tools_reach_history_and_current_auto_read_policy(self):
        first = self.response([
            self.chunk({"tool_calls": [self.tool(1, "read_file", '{"path":"notes', "call_1")]}),
            self.chunk({"tool_calls": [self.tool(0, "read_file", '{"path":"main', "call_0")]}),
            self.chunk({"tool_calls": [{"index": 1, "function": {"arguments": '.txt"}'}},
                                        {"index": 0, "function": {"arguments": '.py"}'}}]}), self.chunk(finish="tool_calls"),
        ])
        _, entries, _, transport, _, approval = self.run_cli([
            first, self.response([self.chunk({"content": "读取完成"}, "stop")]),
        ], args=["--auto-read"])
        approval.assert_not_called()
        next_request = json.loads(transport.call_args_list[1].args[0].data)
        history = next_request["messages"]
        self.assertEqual([message["tool_call_id"] for message in history if message["role"] == "tool"], ["call_0", "call_1"])
        self.assertIn("7391", history[-1]["content"])
        self.assertEqual(sum(entry["event"] == "tool_started" for entry in entries), 2)

    def test_complete_json_without_done_never_executes_and_can_be_recovered(self):
        response = self.response([self.chunk({"tool_calls": [self.tool()]}, "tool_calls")], done=False)
        with patch("learn_agent.tools.run_python") as runner:
            path, entries, text, _, _, approval = self.run_cli([response], error=True)
        self.assertTrue(response.closed)
        runner.assert_not_called()
        approval.assert_not_called()
        self.assertNotIn("model_response", [entry["event"] for entry in entries])
        self.assertEqual((entries[-1]["reason"], entries[-1]["task_state"]), ("error", "failed"))
        self.assertNotIn("最终回答已接收完整", text)
        plan = load_recovery(str(path), self.project)
        plan.require_safe()
        self.assertFalse(plan.tools)
        self.assertEqual(len(plan.history), 1)
        self.assertIn("没有完整回复", plan.note)
        before = path.read_bytes()
        self.run_cli([self.response([self.chunk({"content": "重新回答完成"}, "stop")])], args=["--recover", str(path)])
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_json_arguments_reject_whole_completed_batch(self):
        chunks = [self.chunk({"tool_calls": [self.tool(0, "read_file", '{"path":"notes.txt"}', "call_0"),
                                              self.tool(1, "run_python", '{"path":', "call_1")]}), self.chunk(finish="tool_calls")]
        with patch("learn_agent.tools.read_file") as reader, patch("learn_agent.tools.run_python") as runner:
            _, entries, _, _, _, approval = self.run_cli([self.response(chunks)], error=True)
        for operation in (reader, runner, approval):
            operation.assert_not_called()
        self.assertNotIn("tool_started", [entry["event"] for entry in entries])
        self.assertEqual(entries[-1]["reason"], "error")

    def test_length_finish_never_executes_partial_tool_arguments(self):
        response = self.response([self.chunk({"tool_calls": [self.tool(arguments='{"path":')]}, "length")])
        with patch("learn_agent.tools.run_python") as runner:
            _, entries, _, _, _, approval = self.run_cli([response])
        runner.assert_not_called()
        approval.assert_not_called()
        self.assertEqual(entries[-1]["reason"], "abnormal_finish")

    def test_interrupt_mid_stream_closes_response_and_saves_no_partial_message(self):
        def frames():
            yield from self.event(self.chunk({"content": "尚未完成的文本"}))
            raise KeyboardInterrupt()
        response = StreamResponse(frames())
        _, entries, text, _, _, approval = self.run_cli([response], error=True)
        self.assertTrue(response.closed)
        approval.assert_not_called()
        self.assertIn("尚未完成的文本", text)
        self.assertEqual((entries[-1]["reason"], entries[-1]["task_state"]), ("cancelled", "cancelled"))
        store = SessionStore(self.logs / "sessions.sqlite3")
        self.assertEqual(len(store.read(store.list_sessions()[0].id)[1]), 1)

    def test_http_chunk_interruption_is_saved_as_failed_without_running_tools(self):
        def frames():
            yield from self.event(self.chunk({"tool_calls": [self.tool()]}))
            raise http.client.IncompleteRead(b"partial")
        response = StreamResponse(frames())
        with patch("learn_agent.tools.run_python") as runner:
            _, entries, _, _, _, approval = self.run_cli([response], error=True)
        self.assertTrue(response.closed)
        runner.assert_not_called()
        approval.assert_not_called()
        self.assertEqual(entries[-1]["reason"], "error")

    def test_same_session_restores_completed_stream_and_stream_flag_is_not_inherited(self):
        _, entries, _, _, _, _ = self.run_cli([self.response([self.chunk({"content": "项目名：小灯"}, "stop")])])
        session_id = entries[0]["session_id"]
        normal = {"choices": [{"message": {"role": "assistant", "content": "根据历史回答"}, "finish_reason": "stop"}]}
        _, new, _, transport, _, _ = self.run_cli([io.BytesIO(json.dumps(normal).encode())], args=["--session", session_id], stream=False)
        payload = json.loads(transport.call_args.args[0].data)
        self.assertFalse(payload["stream"])
        self.assertIn("小灯", json.dumps(payload["messages"], ensure_ascii=False))
        self.assertFalse(any(entry["event"] == "model_delta" for entry in new))
        self.assertEqual(new[0]["session_id"], session_id)

    def test_non_stream_transport_remains_compatible(self):
        expected = {"choices": [{"message": {"content": "原有回答"}, "finish_reason": "stop"}]}
        response = io.BytesIO(json.dumps(expected).encode())
        with patch("learn_agent.model.urllib.request.urlopen", return_value=response):
            self.assertEqual(call_model({"stream": False}, "offline-key"), expected)
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
