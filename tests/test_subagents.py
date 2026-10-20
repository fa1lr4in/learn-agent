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
from learn_agent import permissions, tools
from learn_agent.events import load_history
from learn_agent.recovery import load_recovery
from learn_agent.sessions import SessionStore
from learn_agent.subagents import READ_TOOLS
from learn_agent.tasks import replay_events


NAME = "delegate_readonly"


def call(name=NAME, arguments=None, call_id="delegate-1"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments if arguments is not None else {"task": "读取 main.py，说明函数与边界，不运行。"})}}


def read(call_id="read-1"):
    return call("read_file", {"path": "main.py"}, call_id)


def reply(calls=None, content="sum_to 在 main.py 第 1 行；只检查源码，没有运行测试。", usage=True):
    message = {"role": "assistant", "content": "" if calls else content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}],
            **({"usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}} if usage else {})}


class SubagentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-child-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "中文 项目"
        self.project.mkdir()
        (self.project / "main.py").write_text("def sum_to(n):\n    return sum(range(1, n + 1))\n", encoding="utf-8")
        self.logs = self.root / "logs"

    def run_cli(self, replies=(), args=(), answer="y", error=False, prompt="委派只读检查，不修改或运行项目。"):
        before = set(self.logs.glob("*.jsonl"))
        output = io.StringIO()
        requests = []
        iterator = iter(replies) if not callable(replies) else None
        def complete(payload, *other, **kwargs):
            requests.append(copy.deepcopy(payload))
            response = replies(payload, *other, **kwargs) if callable(replies) else next(iterator)
            if isinstance(response, BaseException):
                raise response
            return response
        argv = ["tool_call.py", "--workspace", str(self.project), *args, prompt]
        with patch.object(sys, "argv", argv), patch.object(tool_call, "LOG_DIR", self.logs), \
                patch("tool_call.get_api_key", return_value="offline-placeholder") as key, \
                patch("learn_agent.providers.call_model", side_effect=complete) as model, \
                patch("builtins.input", side_effect=answer if callable(answer) else None,
                      return_value=None if callable(answer) else answer) as approval, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            if error:
                with self.assertRaises(SystemExit) as caught:
                    tool_call.main()
                output.write(str(caught.exception))
            else:
                tool_call.main()
        new_logs = set(self.logs.glob("*.jsonl")) - before
        path = next((p for p in new_logs if ".child-" not in p.name), None)
        entries = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()] if path else []
        return path, entries, requests, model, approval, output.getvalue(), key

    def result(self, events):
        return next(e["result"] for e in events if e["event"] == "tool_result" and e["tool"] == NAME)

    def child_events(self, events):
        path = Path(next(e["child_log"] for e in events if e["event"] == "subagent_start"))
        return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()]

    def success(self, **kwargs):
        return self.run_cli([reply([call()]), reply([read()]), reply(), reply()],
                            ["--subagent", "--auto-read", *kwargs.pop("args", ())], **kwargs)

    def test_default_flow_has_no_delegate_and_no_child_log(self):
        _, events, requests, _, _, _, _ = self.run_cli([reply()])
        self.assertFalse(events[0]["subagent_enabled"])
        self.assertNotIn(NAME, [t["function"]["name"] for t in requests[0]["tools"]])
        self.assertFalse(list(self.logs.glob("*.child-*.jsonl")))

    def test_preview_has_schema_but_never_calls_model_or_creates_child(self):
        path, _, _, model, approval, output, key = self.run_cli(args=["--subagent", "--preview"])
        payload = json.loads(output[output.index("{\n"):output.index("\n预览结束")])
        self.assertIn(NAME, [t["function"]["name"] for t in payload["tools"]])
        self.assertIsNone(path)
        self.assertFalse(self.logs.exists())
        model.assert_not_called()
        approval.assert_not_called()
        key.assert_not_called()

    def test_fresh_context_restricted_tools_evidence_and_shared_usage(self):
        registry = dict(tools.TOOL_REGISTRY)
        path, events, requests, _, approval, _, _ = self.success(prompt="父任务专有信息-7391。请委派一个小检查。")
        result = self.result(events)
        approval.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["usage"]["total_tokens"], 200)
        self.assertFalse(result["code_executed"])
        child_request = requests[1]
        self.assertEqual(len(child_request["messages"]), 2)
        self.assertNotIn("父任务专有信息-7391", json.dumps(child_request, ensure_ascii=False))
        self.assertEqual({t["function"]["name"] for t in child_request["tools"]}, set(READ_TOOLS))
        self.assertEqual(result["evidence"][0]["path"], "main.py")
        self.assertEqual(len(result["evidence"][0]["content_utf8_sha256"]), 64)
        self.assertNotIn("只读项目检查子 Agent", requests[-1]["messages"][0]["content"])
        summary = events[-1]["summary"]
        self.assertEqual((summary["model_calls"], summary["task_model_calls"], summary["subagent_model_calls"]), (4, 2, 2))
        self.assertEqual(summary["usage"]["total_tokens"], 400)
        self.assertEqual(events[-1]["reason"], "stop")
        self.assertEqual(replay_events(events)[0].state, "completed")
        self.assertEqual(replay_events(self.child_events(events))[0].state, "completed")
        self.assertEqual(sum(e["event"] == "model_response" for e in events), 2)
        with permissions.use_workspace(str(self.project)):
            history_from_log = load_history(str(path))
        self.assertEqual(len(history_from_log), 4)
        _, history = SessionStore(self.logs / "sessions.sqlite3").restore(events[0]["session_id"], self.project)
        self.assertEqual(history, history_from_log)
        self.assertEqual(tools.TOOL_REGISTRY, registry)

    def test_parent_project_rules_are_passed_to_child(self):
        (self.project / "rules.md").write_text("报告使用中文，保持现有测试。", encoding="utf-8")
        _, _, requests, _, _, _, _ = self.success(args=["--instructions", "rules.md"])
        self.assertIn("保持现有测试", requests[1]["messages"][-1]["content"])
        self.assertIn("current_user_task", requests[1]["messages"][-1]["content"])

    def test_declined_or_denied_delegation_does_not_start_child(self):
        for extra, answer, reason in (([], "n", "user_denied"), (["--deny-tool", NAME], "y", "policy_denied")):
            with self.subTest(reason=reason):
                _, events, _, model, approval, _, _ = self.run_cli([reply([call()])], ["--subagent", "--auto-read", *extra], answer)
                self.assertFalse(any(e["event"] == "subagent_start" for e in events))
                self.assertEqual(events[-1]["reason"], reason)
                self.assertEqual(approval.call_count, 1 if answer == "n" else 0)
                model.assert_called_once()

    def test_child_does_not_gain_write_run_plugin_or_recursive_tools(self):
        for name in ("write_file", "edit_file", "run_python", "run_command", "delegate_readonly", "plugin_anything"):
            with self.subTest(tool=name), patch("learn_agent.commands.run_command") as command:
                _, events, requests, _, _, _, _ = self.run_cli([reply([call()]), reply([call(name, {"path": "main.py"})]), reply()],
                    ["--subagent", "--auto-read"])
                self.assertFalse(self.result(events)["ok"])
                self.assertEqual(self.result(events)["reason"], "error")
                self.assertFalse(any(e["event"] == "tool_started" for e in self.child_events(events)))
                self.assertNotIn(name, [t["function"]["name"] for t in requests[1]["tools"]])
                command.assert_not_called()
        self.assertEqual((self.project / "main.py").read_text(encoding="utf-8"), "def sum_to(n):\n    return sum(range(1, n + 1))\n")

    def test_child_inherits_read_denial_and_stops_parent(self):
        _, events, _, model, approval, _, _ = self.run_cli([reply([call()]), reply([read()])],
            ["--subagent", "--auto-read", "--deny-tool", "read_file"])
        approval.assert_called_once()
        self.assertEqual(model.call_count, 2)
        self.assertEqual(events[-1]["reason"], "policy_denied")
        self.assertEqual(self.result(events)["halt_reason"], "policy_denied")

    def test_without_auto_read_child_read_still_requires_confirmation(self):
        _, events, _, _, approval, _, _ = self.run_cli([reply([call()]), reply([read()]), reply(), reply()], ["--subagent"])
        self.assertEqual(approval.call_count, 2)
        child = self.child_events(events)
        self.assertEqual(next(e for e in child if e["event"] == "tool_approval")["source"], "user")

    def test_declining_child_read_cancels_parent_and_is_recoverable(self):
        answers = iter(["y", "n"])
        path, events, _, model, _, _, _ = self.run_cli([reply([call()]), reply([read()])], ["--subagent"], lambda _: next(answers))
        self.assertEqual(events[-1]["reason"], "user_denied")
        self.assertEqual(model.call_count, 2)
        recovery = load_recovery(str(path), self.project)
        recovery.require_safe()
        self.assertEqual(recovery.tools[0]["status"], "recorded")

    def test_cancelling_child_model_stops_parent_and_resume_does_not_replay(self):
        path, events, _, model, _, _, _ = self.run_cli([reply([call()]), KeyboardInterrupt()], ["--subagent", "--auto-read"])
        self.assertEqual(model.call_count, 2)
        self.assertEqual(events[-1]["reason"], "cancelled")
        self.assertEqual(replay_events(events)[0].state, "cancelled")
        self.assertEqual(self.child_events(events)[-1]["reason"], "cancelled")
        recovery = load_recovery(str(path), self.project)
        recovery.require_safe()
        _, recovered, requests, _, approval, _, _ = self.run_cli([reply()], ["--recover", str(path)])
        approval.assert_not_called()
        self.assertFalse(any(e["event"] == "subagent_start" for e in recovered))
        self.assertNotIn(NAME, [t["function"]["name"] for t in requests[0]["tools"]])

    def test_child_request_limit_does_not_spend_beyond_four_calls(self):
        _, events, _, model, _, _, _ = self.run_cli(
            [reply([call()]), *[reply([read(f"read-{i}")]) for i in range(4)], reply()],
            ["--subagent", "--auto-read", "--max-model-calls", "10"])
        result = self.result(events)
        self.assertFalse(result["ok"])
        self.assertEqual(result["model_calls"], 4)
        self.assertEqual(result["reason"], "max_model_calls")
        self.assertEqual(sum(e["event"] == "tool_result" for e in self.child_events(events)), 3)
        self.assertEqual(model.call_count, 6)
        self.assertEqual(events[-1]["summary"]["subagent_model_calls"], 4)

    def test_shared_request_limit_preserves_result_and_unexecuted_batch_for_resume(self):
        path, events, _, model, _, _, _ = self.run_cli([reply([call(), read("parent-pending")]), reply([read()]), reply()],
            ["--subagent", "--auto-read", "--max-model-calls", "3"])
        self.assertEqual(model.call_count, 3)
        self.assertEqual(events[-1]["reason"], "max_model_calls")
        self.assertTrue(self.result(events)["ok"])
        self.assertEqual(self.result(events)["halt_reason"], "max_model_calls")
        self.assertEqual([e["tool"] for e in events if e["event"] == "tool_result"], [NAME])
        with permissions.use_workspace(str(self.project)):
            history = load_history(str(path))
        self.assertIn("NotExecuted", history[-1]["content"])
        _, saved = SessionStore(self.logs / "sessions.sqlite3").restore(events[0]["session_id"], self.project)
        self.assertEqual(saved, history)

    def test_shared_token_budget_pauses_in_child_before_reading(self):
        _, events, _, model, _, _, _ = self.run_cli([reply([call()]), reply([read()])],
            ["--subagent", "--auto-read", "--max-total-tokens", "150"])
        self.assertEqual(model.call_count, 2)
        self.assertEqual(events[-1]["reason"], "max_total_tokens")
        self.assertFalse(self.result(events)["ok"])
        self.assertFalse(any(e["event"] == "tool_started" for e in self.child_events(events)))

    def test_missing_child_usage_is_unknown_and_halts_shared_budget(self):
        _, events, _, _, _, _, _ = self.run_cli([reply([call()]), reply([read()], usage=False)],
            ["--subagent", "--auto-read", "--max-total-tokens", "10000"])
        self.assertEqual(events[-1]["reason"], "usage_unavailable")
        self.assertIsNone(self.result(events)["usage"]["total_tokens"])
        self.assertFalse(events[-1]["summary"]["usage_complete"])

    def test_child_report_without_read_evidence_or_too_long_is_not_success(self):
        for responses in ([reply()], [reply([read()]), reply(content="x" * 4001)]):
            with self.subTest(responses=len(responses)):
                _, events, _, _, _, _, _ = self.run_cli([reply([call()]), *responses, reply()], ["--subagent", "--auto-read"])
                self.assertFalse(self.result(events)["ok"])
                self.assertIsNone(self.result(events)["report"])
                self.assertEqual(self.result(events)["error"]["type"], "UnverifiedReport")
                self.assertEqual(replay_events(self.child_events(events))[0].state, "failed")

    def test_invalid_delegation_arguments_block_whole_parent_batch(self):
        for task in ("", " " * 3, True, "x" * 4001):
            with self.subTest(task=str(task)[:10]):
                _, events, _, _, approval, _, _ = self.run_cli([reply([read(), call(arguments={"task": task})])],
                    ["--subagent", "--auto-read"], error=True)
                approval.assert_not_called()
                self.assertFalse(any(e["event"] == "tool_started" for e in events))

    def test_child_path_escape_is_blocked_before_read(self):
        _, events, _, _, _, _, _ = self.run_cli([reply([call()]), reply([call("read_file", {"path": "../outside.txt"})]), reply()],
            ["--subagent", "--auto-read"])
        self.assertFalse(self.result(events)["ok"])
        self.assertFalse(any(e["event"] == "tool_started" for e in self.child_events(events)))

    def test_existing_session_is_not_copied_into_new_child_context(self):
        _, first, _, _, _, _, _ = self.run_cli([reply(content="旧历史秘密 7391")])
        _, events, requests, _, _, _, _ = self.success(args=["--session", first[0]["session_id"]])
        self.assertIn("旧历史秘密 7391", json.dumps(requests[0], ensure_ascii=False))
        self.assertNotIn("旧历史秘密 7391", json.dumps(requests[1], ensure_ascii=False))
        self.assertEqual(events[0]["session_id"], first[0]["session_id"])

    def test_resume_keeps_child_report_but_does_not_enable_or_replay_child(self):
        _, first, _, _, _, _, _ = self.success()
        _, events, requests, _, approval, _, _ = self.run_cli([reply()], ["--session", first[0]["session_id"]])
        approval.assert_not_called()
        self.assertNotIn(NAME, [t["function"]["name"] for t in requests[0]["tools"]])
        self.assertFalse(any(e["event"] == "subagent_start" for e in events))
        self.assertTrue(any(m["role"] == "tool" and "child_id" in m["content"] for m in requests[0]["messages"]))

    def test_child_obeys_context_limit_before_its_first_request(self):
        from learn_agent.budget import RunBudget
        from learn_agent.events import EventRecorder, record_event
        from learn_agent.providers import DeepSeekProvider
        from learn_agent.subagents import make_subagent_tool
        parent = self.root / "parent.jsonl"
        with permissions.use_workspace(str(self.project)), parent.open("w", encoding="utf-8") as f, \
                contextlib.redirect_stdout(io.StringIO()), patch("learn_agent.providers.call_model") as model:
            events = EventRecorder(f)
            record_event(events, "run_start", workspace=str(self.project))
            tool = make_subagent_tool(api_key="offline", budget=RunBudget(8), provider=DeepSeekProvider(),
                                     events=events, log_path=parent, auto_read=True, context_chars=1)
            result = tool.prepare({"task": "读取 main.py"}).execute()
        model.assert_not_called()
        self.assertEqual(result["reason"], "context_limit")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
