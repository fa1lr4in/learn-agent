import copy
import io
import json
import tempfile
import time
import unittest
import sys
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient

import api as api_cli
from learn_agent import permissions
from learn_agent.api import create_app
from learn_agent.providers import DeepSeekProvider, ModelResponse
from learn_agent.sessions import SessionStore
from learn_agent.tasks import replay_events


KEY = "offline-model-key-must-stay-in-backend"
TOKEN = "offline-local-access-token"


def call(name, arguments, call_id="call-1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def reply(*calls, text="完成。", finish=None):
    return ModelResponse({"role": "assistant", "content": text, **({"tool_calls": list(calls)} if calls else {})},
                         finish or ("tool_calls" if calls else "stop"),
                         {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})


class OfflineProvider(DeepSeekProvider):
    def __init__(self):
        self.replies = []
        self.requests = []

    def complete(self, payload, api_key, on_delta=None):
        self.requests.append(copy.deepcopy(payload))
        response = self.replies.pop(0)
        if isinstance(response, Exception):
            raise response
        return response() if callable(response) else response


class LocalApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-api-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "中文 项目"
        self.project.mkdir()
        (self.project / "main.py").write_text("def sum_to(n):\n    return sum(range(1, n + 1))\n\nif __name__ == '__main__':\n    print(sum_to(100))\n", encoding="utf-8")
        (self.project / "test_main.py").write_text(
            "import unittest\nfrom main import sum_to\nclass SumTests(unittest.TestCase):\n"
            "    def test_zero(self): self.assertEqual(sum_to(0), 0)\n"
            "    def test_one(self): self.assertEqual(sum_to(1), 1)\n"
            "    def test_ten(self): self.assertEqual(sum_to(10), 55)\n"
            "    def test_hundred(self): self.assertEqual(sum_to(100), 5050)\n", encoding="utf-8")
        self.output = patch("sys.stdout", new=io.StringIO())
        self.output.start()
        self.addCleanup(self.output.stop)
        self.provider = OfflineProvider()
        self.logs = self.root / "logs"
        self.app = create_app(self.project, self.logs, KEY, TOKEN, provider=self.provider)
        self.manager = self.app.state.manager
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8000", headers={"Authorization": f"Bearer {TOKEN}"})
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def start(self, **options):
        session = self.client.post("/sessions").json()
        response = self.client.post(f"/sessions/{session['id']}/tasks", json={"prompt": "验证当前项目。", **options})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def wait(self, task, predicate=lambda t: t["finished"]):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            response = self.client.get(f"/tasks/{task['id']}")
            self.assertEqual(response.status_code, 200)
            snapshot = response.json()
            if predicate(snapshot):
                return snapshot
            time.sleep(.01)
        self.fail(f"等待任务超时：{snapshot}")

    def waiting(self, task):
        return self.wait(task, lambda t: t["pending_approval"] is not None)

    def decide(self, task, approved):
        return self.client.post(f"/tasks/{task['id']}/approvals", json={
            "approval_id": task["pending_approval"]["id"], "approved": approved})

    def test_auth_host_origin_and_public_health(self):
        self.assertEqual(self.client.get("/health", headers={"Authorization": ""}).json(), {"status": "ok", "api_version": 1})
        self.assertIn("charset=utf-8", self.client.get("/health").headers["content-type"])
        for headers, expected in [({"Authorization": ""}, 401), ({"Authorization": "Bearer wrong"}, 401),
                                  ({"Origin": "https://untrusted.example"}, 403), ({"Origin": "null"}, 403),
                                  ({"Host": "untrusted.example"}, 400)]:
            with self.subTest(headers=headers):
                self.assertEqual(self.client.post("/sessions", headers=headers).status_code, expected)
        self.assertEqual(self.client.post("/sessions", headers={"Origin": "http://127.0.0.1:8000"}).status_code, 201)
        self.assertNotIn("access-control-allow-origin", self.client.get("/health").headers)

    def test_validation_rejects_invalid_budgets_keys_and_workspace(self):
        session = self.client.post("/sessions").json()["id"]
        for body in [{"prompt": " "}, {"prompt": "x", "max_model_calls": 0},
                     {"prompt": "x", "max_model_calls": True}, {"prompt": "x", "max_model_calls": "8"},
                     {"prompt": "x", "max_total_tokens": -1}, {"prompt": "x", "auto_read": "yes"},
                     {"prompt": "x", "workspace": str(self.root)}, {"prompt": "x", "api_key": "not-accepted"},
                     {"prompt": "x", "denied_tools": ["unknown"]}]:
            with self.subTest(body=body):
                self.assertEqual(self.client.post(f"/sessions/{session}/tasks", json=body).status_code, 422)
        self.assertEqual(self.provider.requests, [])
        self.assertFalse(list(self.logs.glob("*.jsonl")))

    def test_instance_identity_requires_auth_and_never_contains_credentials(self):
        self.assertEqual(self.client.get("/instance", headers={"Authorization": ""}).status_code, 401)
        identity = self.client.get("/instance").json()
        self.assertEqual(identity["workspace"], str(self.project.resolve()))
        self.assertEqual(identity["api_version"], 1)
        self.assertIsInstance(identity["pid"], int)
        self.assertTrue(identity["instance_id"])
        self.assertNotIn(KEY, json.dumps(identity))
        self.assertNotIn(TOKEN, json.dumps(identity))

    def test_unknown_ids_are_not_found(self):
        for path in ["/tasks/unknown", "/sessions/unknown"]:
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.post("/sessions/unknown/tasks", json={"prompt": "x"}).status_code, 404)
        self.assertEqual(self.client.post("/tasks/unknown/cancel").status_code, 404)

    def test_openapi_documents_auth_and_requests_without_model_key(self):
        schema = self.client.get("/openapi.json").json()
        self.assertIn("HTTPBearer", schema["components"]["securitySchemes"])
        self.assertNotIn("api_key", schema["components"]["schemas"]["TaskRequest"]["properties"])
        self.assertEqual(self.client.get("/docs").status_code, 200)

    def test_read_execute_two_approvals_usage_log_and_sqlite_agree(self):
        original = permissions.WORKSPACE
        self.provider.replies = [
            reply(call("read_file", {"path": "main.py"}, "read-main"), call("read_file", {"path": "test_main.py"}, "read-tests")),
            reply(call("run_command", {"command": ["python", "-m", "unittest", "-v", "test_main.py"]}, "tests"),
                  call("run_python", {"path": "main.py"}, "entry")), reply(text="四项测试通过，入口为 5050。")]
        task = self.waiting(self.start(auto_read=True))
        self.assertEqual(task["pending_approval"]["tool"], "run_command")
        self.assertEqual(len([e for e in task["events"] if e["event"] == "tool_result"]), 2)
        first_approval = task["pending_approval"]["id"]
        self.assertEqual(self.decide(task, True).status_code, 202)
        task = self.wait(task, lambda t: t["pending_approval"] is not None and t["pending_approval"]["id"] != first_approval)
        self.assertEqual(task["pending_approval"]["tool"], "run_python")
        self.assertEqual(self.client.post(f"/tasks/{task['id']}/approvals", json={"approval_id": first_approval, "approved": True}).status_code, 409)
        self.assertEqual(self.decide(task, True).status_code, 202)
        task = self.wait(task)
        self.assertEqual((task["state"], task["reason"], task["session_saved"]), ("completed", "stop", True))
        results = [e["result"] for e in task["events"] if e["event"] == "tool_result"]
        self.assertIn("Ran 4 tests", results[2]["stderr"])
        self.assertIn("OK", results[2]["stderr"])
        self.assertEqual(results[3]["stdout"].strip(), "5050")
        self.assertEqual(task["summary"]["usage"], {"prompt_tokens": 30, "completion_tokens": 6, "total_tokens": 36})
        self.assertEqual(task["summary"]["model_calls"], 3)
        disk = [json.loads(line) for line in Path(task["log_path"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(disk, task["events"])
        self.assertEqual(replay_events(disk)[0].state, "completed")
        session = self.client.get(f"/sessions/{task['session_id']}").json()
        self.assertEqual(session["messages"][-1]["content"], task["final_answer"])
        self.assertEqual([t["approval_source"] for t in session["tool_runs"]], ["auto_read", "auto_read", "user", "user"])
        self.assertEqual(permissions.WORKSPACE, original)
        self.assertTrue(Path(task["log_path"]).with_suffix(".checkpoint.json").exists())
        self.assertNotIn(KEY, json.dumps(task))
        self.assertNotIn(TOKEN, json.dumps(task))

    def test_deny_leaves_files_and_later_tools_unexecuted(self):
        self.provider.replies = [reply(call("write_file", {"path": "unwanted.txt", "content": "no"}, "write"),
                                       call("run_python", {"path": "main.py"}, "run"))]
        task = self.waiting(self.start())
        self.assertEqual(self.decide(task, False).status_code, 202)
        task = self.wait(task)
        self.assertEqual((task["state"], task["reason"]), ("cancelled", "user_denied"))
        self.assertFalse((self.project / "unwanted.txt").exists())
        self.assertFalse(any(e["event"] == "tool_result" for e in task["events"]))
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(self.decide({**task, "pending_approval": {"id": "old"}}, True).status_code, 409)

    def test_approval_is_bound_to_task_and_strict_boolean(self):
        self.provider.replies = [reply(call("read_file", {"path": "main.py"}))]
        task = self.waiting(self.start())
        second = self.client.post("/sessions").json()["id"]
        self.assertEqual(self.client.post(f"/sessions/{second}/tasks", json={"prompt": "x"}).status_code, 409)
        path = f"/tasks/{task['id']}/approvals"
        self.assertEqual(self.client.post(path, json={"approval_id": "wrong", "approved": True}).status_code, 409)
        self.assertEqual(self.client.post(path, json={"approval_id": task["pending_approval"]["id"], "approved": "true"}).status_code, 422)
        self.assertEqual(self.client.post("/tasks/other/approvals", json={"approval_id": task["pending_approval"]["id"], "approved": True}).status_code, 404)
        self.assertFalse(any(e["event"] == "tool_result" for e in self.manager.snapshot(task["id"])["events"]))

    def test_cancel_waiting_wakes_worker_and_never_executes(self):
        self.provider.replies = [reply(call("run_python", {"path": "main.py"}))]
        task = self.waiting(self.start())
        self.assertEqual(self.client.post(f"/tasks/{task['id']}/cancel").status_code, 202)
        task = self.wait(task)
        self.assertEqual((task["state"], task["reason"], task["session_saved"]), ("cancelled", "cancelled", True))
        self.assertFalse(any(e["event"] in ("tool_approval", "tool_started", "tool_result") for e in task["events"]))
        self.assertEqual(len(self.provider.requests), 1)

    def test_cancel_during_model_waits_for_response_and_skips_tools(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        def delayed():
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return reply(call("write_file", {"path": "not-created.txt", "content": "no"}))
        self.provider.replies = [delayed]
        task = self.start(auto_read=True)
        self.assertTrue(entered.wait(5))
        self.client.post(f"/tasks/{task['id']}/cancel")
        self.assertFalse(self.client.get(f"/tasks/{task['id']}").json()["finished"])
        release.set()
        task = self.wait(task)
        self.assertEqual(task["reason"], "cancelled")
        self.assertEqual(task["summary"]["model_calls"], 1)
        self.assertFalse(any(e["event"].startswith("tool_") for e in task["events"]))
        self.assertFalse((self.project / "not-created.txt").exists())

    def test_explicit_policy_denial_cannot_be_overridden_by_auto_read(self):
        self.provider.replies = [reply(call("read_file", {"path": "main.py"}))]
        task = self.wait(self.start(auto_read=True, denied_tools=["read_file"]))
        self.assertEqual((task["state"], task["reason"]), ("cancelled", "policy_denied"))
        self.assertFalse(any(e["event"] in ("tool_waiting", "tool_result") for e in task["events"]))

    def test_request_limit_pauses_without_executing_and_resume_does_not_replay(self):
        self.provider.replies = [reply(call("read_file", {"path": "main.py"})), reply(text="继续回答。")]
        task = self.wait(self.start(auto_read=True, max_model_calls=1))
        self.assertEqual((task["state"], task["reason"]), ("paused", "max_model_calls"))
        self.assertFalse(any(e["event"] == "tool_result" for e in task["events"]))
        resumed = self.client.post(f"/sessions/{task['session_id']}/tasks", json={"prompt": "继续，只回答。"})
        self.assertEqual(resumed.status_code, 202)
        resumed = self.wait(resumed.json())
        self.assertEqual(resumed["reason"], "stop")
        self.assertIn("NotExecuted", json.dumps(self.provider.requests[-1]))
        self.assertFalse(any(e["event"] == "tool_result" for e in resumed["events"]))

    def test_token_budget_counts_response_before_pausing_tools(self):
        self.provider.replies = [reply(call("read_file", {"path": "main.py"}))]
        task = self.wait(self.start(auto_read=True, max_total_tokens=1))
        self.assertEqual((task["state"], task["reason"]), ("paused", "max_total_tokens"))
        self.assertEqual(task["summary"]["usage"]["total_tokens"], 12)
        self.assertFalse(any(e["event"] == "tool_result" for e in task["events"]))

    def test_session_continues_with_history_after_server_restart(self):
        self.provider.replies = [reply(text="项目叫小灯。"), reply(text="项目名是小灯。")]
        task = self.wait(self.start())
        self.manager.close()
        app = create_app(self.project, self.logs, KEY, "new-token", provider=self.provider)
        with TestClient(app, base_url="http://127.0.0.1:8000", headers={"Authorization": "Bearer new-token"}) as client:
            self.assertEqual(client.get("/sessions").json()[0]["id"], task["session_id"])
            self.assertEqual(client.get(f"/tasks/{task['id']}").status_code, 404)
            response = client.post(f"/sessions/{task['session_id']}/tasks", json={"prompt": "项目叫什么？"})
            self.assertEqual(response.status_code, 202)
            with app.state.manager.condition:
                self.assertTrue(app.state.manager.condition.wait_for(lambda: app.state.manager.tasks[response.json()["id"]].finished, timeout=5))
            self.assertEqual(client.get(f"/tasks/{response.json()['id']}").json()["reason"], "stop")
        messages = self.provider.requests[-1]["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertEqual(messages[2]["content"], "项目叫小灯。")

    def test_cancelled_session_requires_explicit_recovery_instead_of_blind_replay(self):
        self.provider.replies = [reply(call("read_file", {"path": "main.py"}))]
        task = self.waiting(self.start())
        self.client.post(f"/tasks/{task['id']}/cancel")
        task = self.wait(task)
        self.assertEqual(self.client.post(f"/sessions/{task['session_id']}/tasks", json={"prompt": "继续"}).status_code, 409)

    def test_workspace_escape_is_rejected_without_approval_or_read(self):
        self.provider.replies = [reply(call("read_file", {"path": "../outside.txt"}))]
        task = self.wait(self.start(auto_read=True))
        self.assertEqual((task["state"], task["reason"], task["error_type"]), ("failed", "error", "ValueError"))
        self.assertFalse(any(e["event"] in ("tool_waiting", "tool_result") for e in task["events"]))

    def test_approved_edit_uses_real_checkpoint_and_one_replacement(self):
        self.provider.replies = [reply(call("edit_file", {"path": "main.py", "old_text": "sum_to(100)", "new_text": "sum_to(10)"})), reply()]
        task = self.waiting(self.start())
        self.assertEqual(task["pending_approval"]["arguments"]["new_text"], "sum_to(10)")
        self.decide(task, True)
        task = self.wait(task)
        self.assertEqual(task["state"], "completed")
        self.assertIn("print(sum_to(10))", (self.project / "main.py").read_text(encoding="utf-8"))
        result = next(e["result"] for e in task["events"] if e["event"] == "tool_result")
        self.assertEqual(result["replacements"], 1)
        checkpoint = json.loads(Path(task["log_path"]).with_suffix(".checkpoint.json").read_text(encoding="utf-8"))
        self.assertIn("main.py", checkpoint["files"])

    def test_model_error_is_failed_and_does_not_echo_secret(self):
        self.provider.replies = [OSError(KEY)]
        task = self.wait(self.start())
        self.assertEqual((task["state"], task["reason"], task["error_type"]), ("failed", "error", "OSError"))
        self.assertTrue(task["session_saved"])
        self.assertNotIn(KEY, json.dumps(task))

    def test_persistence_error_is_visible_and_does_not_claim_session_saved(self):
        self.provider.replies = [reply()]
        with patch.object(SessionStore, "save_run", side_effect=OSError(KEY)):
            task = self.wait(self.start())
        self.assertEqual((task["reason"], task["session_saved"], task["persistence_error"]), ("stop", False, "OSError"))
        self.assertTrue(Path(task["log_path"]).exists())
        self.assertNotIn(KEY, json.dumps(task))

    def test_other_workspace_sessions_are_not_exposed(self):
        self.provider.replies = [reply()]
        task = self.wait(self.start())
        self.manager.close()
        app = create_app(self.root, self.logs, KEY, TOKEN)
        with TestClient(app, base_url="http://127.0.0.1:8000", headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            self.assertEqual(client.get("/sessions").json(), [])
            self.assertEqual(client.get(f"/sessions/{task['session_id']}").status_code, 404)

    def test_shutdown_wakes_pending_task_and_releases_core_for_next_task(self):
        self.provider.replies = [reply(call("read_file", {"path": "main.py"}))]
        task = self.waiting(self.start())
        self.manager.close()
        task = self.client.get(f"/tasks/{task['id']}").json()
        self.assertEqual((task["finished"], task["reason"], task["session_saved"]), (True, "cancelled", True))
        self.assertFalse(any(e["event"] == "tool_result" for e in task["events"]))
        self.assertEqual(self.client.post("/sessions").status_code, 409)

    def test_second_app_cannot_change_workspace_during_active_task(self):
        self.provider.replies = [reply(call("read_file", {"path": "main.py"}))]
        task = self.waiting(self.start())
        other_provider = OfflineProvider()
        other_provider.replies = [reply()]
        other = create_app(self.root, self.root / "other-logs", KEY, TOKEN, provider=other_provider)
        with TestClient(other, base_url="http://127.0.0.1:8000", headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            session = client.post("/sessions").json()["id"]
            self.assertEqual(client.post(f"/sessions/{session}/tasks", json={"prompt": "x"}).status_code, 409)
            self.assertEqual(permissions.WORKSPACE, self.project)
            self.assertEqual(other_provider.requests, [])
            self.client.post(f"/tasks/{task['id']}/cancel")
            self.wait(task)
            response = client.post(f"/sessions/{session}/tasks", json={"prompt": "x"})
            self.assertEqual(response.status_code, 202)
            with other.state.manager.condition:
                self.assertTrue(other.state.manager.condition.wait_for(lambda: other.state.manager.tasks[response.json()["id"]].finished, timeout=5))

    def test_launcher_rejects_missing_model_key_without_writing_configuration(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}), patch.object(sys, "argv", ["api.py"]), \
                patch.object(api_cli, "LOG_DIR", self.root / "launch-logs"):
            with self.assertRaisesRegex(SystemExit, "DEEPSEEK_API_KEY"):
                api_cli.main()
        self.assertFalse((self.root / "launch-logs").exists())

    def test_launcher_binds_loopback_and_only_saves_temporary_local_token(self):
        launch_logs = self.root / "launch-logs"
        info_path = launch_logs / ".api-server.json"
        def inspect_launch(sockets):
            info = json.loads(info_path.read_text(encoding="utf-8"))
            self.assertEqual(info["url"], "http://127.0.0.1:8000")
            self.assertGreaterEqual(len(info["token"]), 32)
            self.assertNotIn(KEY, info_path.read_text(encoding="utf-8"))
            config = server.call_args.args[0]
            self.assertEqual((config.host, config.port), ("127.0.0.1", 8000))
            config.app.state.manager.close()
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": KEY}), patch.object(sys, "argv", ["api.py", "--workspace", str(self.project)]), \
                patch.object(api_cli, "LOG_DIR", launch_logs), patch.object(api_cli.socket, "socket") as socket_factory, \
                patch.object(api_cli.uvicorn, "Server") as server:
            server.return_value.run.side_effect = inspect_launch
            api_cli.main()
            socket_factory.return_value.__enter__.return_value.bind.assert_called_once_with(("127.0.0.1", 8000))
            server.return_value.run.side_effect = KeyboardInterrupt
            api_cli.main()
            server.call_args.args[0].app.state.manager.close()
        self.assertFalse(info_path.exists())
        with permissions.use_workspace(self.root):
            with self.assertRaises(ValueError):
                permissions.resolve_workspace_path("launch-logs/.api-server.json")


if __name__ == "__main__":
    unittest.main()
