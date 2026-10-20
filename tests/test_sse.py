import io
import json
import socket
import tempfile
import time
import unittest
import urllib.request
from email.message import Message
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

import httpx
import uvicorn

import watch_task
from learn_agent.api import create_app
from learn_agent.sessions import SessionStore
from test_api import KEY, TOKEN, OfflineProvider, call, reply


class DeltaProvider(OfflineProvider):
    def complete(self, payload, api_key, on_delta=None):
        response = super().complete(payload, api_key, on_delta)
        if on_delta is not None:
            on_delta({"content": response.message.get("content") or ""})
        return response


def wire(frame):
    return f"id: {frame['id']}\nevent: {frame['event']}\ndata: {json.dumps(frame, ensure_ascii=False)}\n\n".encode("utf-8")


class SseProtocolTests(unittest.TestCase):
    def test_parser_handles_comments_multiline_json_and_chinese(self):
        raw = [b"\xef\xbb\xbf: connected\r\n", b"retry: 1000\r\n", b"\r\n", b"id: task:1\n", b"event: note\n",
               b'data: {"id":"task:1", "event":"note",\n', 'data: "data":{"text":"中文\\n第二行"}}\n'.encode(), b"\n"]
        frames = list(watch_task.parse_events(raw))
        self.assertEqual(frames[0]["data"]["text"], "中文\n第二行")
        self.assertEqual(len(frames), 1)

    def test_incomplete_frame_is_not_delivered(self):
        frame = {"id": "task:1", "event": "note", "data": {}}
        self.assertEqual(list(watch_task.parse_events(wire(frame).rstrip(b"\n").splitlines(keepends=True))), [])

    def test_wire_id_and_payload_must_agree(self):
        with self.assertRaisesRegex(ValueError, "不一致"):
            list(watch_task.parse_events([b"id: task:1\n", b"event: note\n", b'data: {"id":"other:1","event":"note"}\n', b"\n"]))

    def test_cursor_cannot_cross_tasks_or_accept_negative_sequences(self):
        self.assertEqual(watch_task.event_sequence("task:0", "task"), 0)
        for cursor in ("other:1", "task:-1", "task:one", "task:１", "task", "task:1234567890123"):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                watch_task.event_sequence(cursor, "task")

    def test_watcher_reconnects_incomplete_tail_even_when_task_has_ended(self):
        task_id = "a" * 32
        snapshot = {"id": f"{task_id}:1", "event": "snapshot", "task_id": task_id,
                    "data": {"state": "running", "reason": None, "finished": False, "events": []}}
        finished = {"id": f"{task_id}:2", "event": "task_finished", "task_id": task_id,
                    "data": {"state": "completed", "reason": "stop", "finished": True}}
        def response(content, content_type):
            stream = io.BytesIO(content)
            stream.headers = Message()
            stream.headers["Content-Type"] = content_type
            return stream
        responses = [response(wire(snapshot) + wire(finished)[:-2], "text/event-stream"),
                     response(json.dumps({"finished": True, "last_event_id": finished["id"]}).encode(), "application/json"),
                     response(wire(finished), "text/event-stream")]
        with tempfile.TemporaryDirectory() as directory, patch.object(watch_task.urllib.request, "urlopen", side_effect=responses) as network, \
                patch.object(watch_task.time, "sleep"), patch("sys.stdout", new=io.StringIO()):
            cursor = Path(directory) / "cursor.json"
            watch_task.watch({"url": "http://127.0.0.1:8000", "token": TOKEN}, task_id, cursor)
            self.assertEqual(json.loads(cursor.read_text())["last_event_id"], finished["id"])
            self.assertEqual(network.call_args_list[2].args[0].get_header("Last-event-id"), snapshot["id"])

    def test_watcher_rejects_foreign_url_before_sending_token(self):
        with patch.object(watch_task.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(ValueError, "仅连接本机"):
                watch_task.watch({"url": "https://other.example", "token": TOKEN}, "a" * 32, Path("unused"))
            network.assert_not_called()


class SseHttpTests(unittest.TestCase):
    """Use real loopback sockets: in-process test clients can buffer an entire stream."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-sse-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "main.py").write_text("print(5050)\n", encoding="utf-8")
        output = patch("sys.stdout", new=io.StringIO())
        output.start()
        self.addCleanup(output.stop)
        self.provider = DeltaProvider()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(128)
        self.port = self.listener.getsockname()[1]
        self.app = create_app(self.project, self.root / "logs", KEY, TOKEN, port=self.port, provider=self.provider)
        self.manager = self.app.state.manager
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="critical",
                                                   access_log=False, timeout_graceful_shutdown=.2))
        self.worker = Thread(target=self.server.run, kwargs={"sockets": [self.listener]}, daemon=True)
        self.worker.start()
        self.addCleanup(self.close)
        self.url = f"http://127.0.0.1:{self.port}"
        self.client = httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
        self.addCleanup(self.client.close)
        deadline = time.monotonic() + 8
        while not self.server.started:
            if time.monotonic() > deadline:
                self.fail("测试后端没有启动。")
            time.sleep(.01)

    def close(self):
        self.server.should_exit = True
        self.worker.join(15)
        self.listener.close()
        if self.worker.is_alive():
            self.fail("测试后端未退出。")

    def start(self, **options):
        session = self.client.post("/sessions").json()["id"]
        response = self.client.post(f"/sessions/{session}/tasks", json={"prompt": "只验证入口。", "stream": True, **options})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def wait(self, task, predicate=lambda t: t["finished"]):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.client.get(f"/tasks/{task['id']}").json()
            if predicate(result):
                return result
            time.sleep(.01)
        self.fail(f"等待 SSE 测试任务超时：{result}")

    def stream(self, task, cursor=None, suffix=""):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        if cursor is not None:
            headers["Last-Event-ID"] = cursor
        return urllib.request.urlopen(urllib.request.Request(f"{self.url}/tasks/{task['id']}/events{suffix}", headers=headers), timeout=5)

    def approve(self, task):
        response = self.client.post(f"/tasks/{task['id']}/approvals", json={"approval_id": task["pending_approval"]["id"], "approved": True})
        self.assertEqual(response.status_code, 202, response.text)

    def test_disconnect_resume_and_terminal_event_do_not_rerun_task(self):
        self.provider.replies = [reply(call("run_python", {"path": "main.py"})), reply(text="实际输出为 5050。")]
        task = self.wait(self.start(), lambda t: t["pending_approval"] is not None)
        with self.stream(task) as response:
            self.assertTrue(response.headers.get_content_type() == "text/event-stream")
            self.assertEqual(response.headers["X-Accel-Buffering"], "no")
            first = next(watch_task.parse_events(response))
        self.assertEqual(first["event"], "snapshot")
        self.assertEqual(first["data"]["pending_approval"]["id"], task["pending_approval"]["id"])
        self.assertEqual(first["id"], first["data"]["last_event_id"])
        still_waiting = self.client.get(f"/tasks/{task['id']}").json()
        self.assertFalse(still_waiting["cancel_requested"])
        self.assertEqual(len(self.provider.requests), 1)
        self.approve(still_waiting)
        task = self.wait(task)
        with self.stream(task, first["id"]) as response:
            frames = list(watch_task.parse_events(response))
        sequence = int(first["id"].rsplit(":", 1)[1])
        self.assertEqual([int(f["id"].rsplit(":", 1)[1]) for f in frames], list(range(sequence + 1, sequence + len(frames) + 1)))
        self.assertEqual(frames[-1]["event"], "task_finished")
        self.assertTrue(frames[-1]["data"]["session_saved"])
        self.assertEqual(frames[-1]["data"]["final_answer"], "实际输出为 5050。")
        self.assertEqual(task["last_event_id"], frames[-1]["id"])
        self.assertEqual(len(self.provider.requests), 2)
        self.assertEqual(task["summary"]["usage"]["total_tokens"], 24)
        self.assertTrue(self.provider.requests[0]["stream"])
        self.assertTrue(any(e["event"] == "model_delta" for e in task["events"]))
        self.assertNotIn(KEY, json.dumps(frames))
        self.assertNotIn(TOKEN, json.dumps(frames))

    def test_snapshot_cursor_covers_events_emitted_before_subscription(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        def delayed():
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return reply(call("run_python", {"path": "main.py"}))
        self.provider.replies = [delayed]
        task = self.start()
        self.assertTrue(entered.wait(5))
        snapshot = self.client.get(f"/tasks/{task['id']}").json()
        release.set()
        self.wait(task, lambda t: t["pending_approval"] is not None)
        with self.stream(task, suffix=f"?after={snapshot['last_event_id']}") as response:
            frames = []
            for frame in watch_task.parse_events(response):
                frames.append(frame)
                if frame["event"] == "approval_required":
                    break
        self.assertNotIn("snapshot", [f["event"] for f in frames])
        start = int(snapshot["last_event_id"].rsplit(":", 1)[1])
        self.assertEqual([int(f["id"].rsplit(":", 1)[1]) for f in frames], list(range(start + 1, start + len(frames) + 1)))
        pending = self.client.get(f"/tasks/{task['id']}").json()["pending_approval"]
        self.assertEqual(frames[-1]["data"], pending)
        self.client.post(f"/tasks/{task['id']}/cancel")
        self.wait(task)

    def test_two_observers_receive_same_events_without_consuming_each_other(self):
        self.provider.replies = [reply(call("run_python", {"path": "main.py"})), reply()]
        task = self.wait(self.start(), lambda t: t["pending_approval"] is not None)
        with self.stream(task) as first, self.stream(task) as second:
            one, two = watch_task.parse_events(first), watch_task.parse_events(second)
            self.assertEqual(next(one)["id"], next(two)["id"])
            self.approve(task)
            self.assertEqual(list(one), list(two))
        self.assertEqual(len(self.provider.requests), 2)

    def test_stream_access_and_invalid_cursors_fail_before_streaming(self):
        self.provider.replies = [reply()]
        task = self.wait(self.start())
        path = f"/tasks/{task['id']}/events"
        for query, headers, status in [({}, {"Authorization": ""}, 401), ({}, {"Origin": "https://other.example"}, 403),
                                      ({"after": "bad"}, {}, 422), ({"after": f"{task['id']}:-1"}, {}, 422),
                                      ({"after": "other:1"}, {}, 409), ({"after": f"{task['id']}:999999"}, {}, 409),
                                      ({"after": f"{task['id']}:0"}, {"Last-Event-ID": task["last_event_id"]}, 422)]:
            with self.subTest(query=query, headers=headers):
                response = self.client.get(path, params=query, headers=headers)
                self.assertEqual(response.status_code, status, response.text)
                self.assertNotIn("text/event-stream", response.headers["content-type"])
        self.assertEqual(self.client.get("/tasks/unknown/events").status_code, 404)

    def test_replay_from_zero_and_final_cursor_close_cleanly(self):
        self.provider.replies = [reply()]
        task = self.wait(self.start())
        with self.stream(task, task["id"] + ":0") as response:
            frames = list(watch_task.parse_events(response))
        self.assertEqual(frames[0]["event"], "run_start")
        self.assertEqual(frames[-1]["event"], "task_finished")
        self.assertEqual([f["id"] for f in frames], [f"{task['id']}:{i}" for i in range(1, len(frames) + 1)])
        self.assertEqual([f["data"] for f in frames if f["event"] not in ("task_finished",)], task["events"])
        with self.stream(task, task["last_event_id"]) as response:
            self.assertEqual(response.read(), b"")
        with self.stream(task) as response:
            fresh = list(watch_task.parse_events(response))
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["data"], task)

    def test_other_task_cursor_is_rejected_even_with_same_sequence(self):
        self.provider.replies = [reply(), reply()]
        first = self.wait(self.start())
        second = self.wait(self.start())
        response = self.client.get(f"/tasks/{second['id']}/events", headers={"Last-Event-ID": first["last_event_id"]})
        self.assertEqual(response.status_code, 409)
        with self.stream(second, second["id"] + ":0") as response:
            frames = list(watch_task.parse_events(response))
        self.assertTrue(all(f["task_id"] == second["id"] and f["session_id"] == second["session_id"] for f in frames))

    def test_final_notification_waits_for_database_save(self):
        saving, release = Event(), Event()
        self.addCleanup(release.set)
        def delayed_save(*args, **kwargs):
            saving.set()
            if not release.wait(5):
                raise TimeoutError
            raise OSError(KEY)
        self.provider.replies = [reply()]
        with patch.object(SessionStore, "save_run", side_effect=delayed_save):
            task = self.start()
            self.assertTrue(saving.wait(5))
            with self.stream(task) as response:
                frames = watch_task.parse_events(response)
                snapshot = next(frames)
                self.assertEqual(snapshot["data"]["events"][-1]["event"], "run_end")
                self.assertFalse(snapshot["data"]["finished"])
                release.set()
                final = list(frames)[-1]
            self.assertEqual(final["event"], "task_finished")
            self.assertFalse(final["data"]["session_saved"])
            self.assertEqual(final["data"]["persistence_error"], "OSError")
            self.assertNotIn(KEY, json.dumps(final))

    def test_keepalive_does_not_advance_task_cursor(self):
        self.provider.replies = [reply(call("run_python", {"path": "main.py"}))]
        task = self.wait(self.start(), lambda t: t["pending_approval"] is not None)
        with patch("fastapi.routing._PING_INTERVAL", .05), self.stream(task) as response:
            snapshot = next(watch_task.parse_events(response))
            self.assertTrue(response.readline().startswith(b":"))
        self.assertEqual(self.client.get(f"/tasks/{task['id']}").json()["last_event_id"], snapshot["id"])
        self.client.post(f"/tasks/{task['id']}/cancel")
        self.wait(task)

    def test_budget_pause_is_terminal_and_has_no_tool_execution(self):
        self.provider.replies = [reply(call("run_python", {"path": "main.py"}))]
        task = self.wait(self.start(max_model_calls=1))
        with self.stream(task, task["id"] + ":0") as response:
            frames = list(watch_task.parse_events(response))
        self.assertEqual(frames[-1]["data"]["reason"], "max_model_calls")
        self.assertEqual(frames[-1]["data"]["summary"]["model_calls"], 1)
        self.assertFalse(any(f["event"] in ("approval_required", "tool_result") for f in frames))

    def test_watch_client_saves_cursor_and_resumes_without_new_model_calls(self):
        self.provider.replies = [reply(call("run_python", {"path": "main.py"})), reply()]
        task = self.wait(self.start(), lambda t: t["pending_approval"] is not None)
        info = {"url": self.url, "token": TOKEN}
        client_logs = self.root / "logs"
        (client_logs / ".api-server.json").write_text(json.dumps(info), encoding="utf-8")
        cursor_path = client_logs / f".watch-{task['id']}.json"
        with patch.object(watch_task, "LOG_DIR", client_logs), patch("sys.argv", ["watch_task.py", task["id"], "--limit", "1"]):
            watch_task.main()
        cursor = json.loads(cursor_path.read_text())["last_event_id"]
        self.assertEqual(cursor, self.client.get(f"/tasks/{task['id']}").json()["last_event_id"])
        self.approve(task)
        task = self.wait(task)
        with patch.object(watch_task, "LOG_DIR", client_logs), patch("sys.argv", ["watch_task.py", task["id"], "--resume"]):
            watch_task.main()
        self.assertEqual(json.loads(cursor_path.read_text())["last_event_id"], task["last_event_id"])
        with patch.object(watch_task, "LOG_DIR", client_logs), patch("sys.argv", ["watch_task.py", task["id"], "--resume"]):
            watch_task.main()
        self.assertEqual(len(self.provider.requests), 2)


if __name__ == "__main__":
    unittest.main()
