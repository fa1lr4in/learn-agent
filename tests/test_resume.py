import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="learn-agent-test-")
        self.root = Path(self.directory.name)
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.directory.cleanup)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        log_patch = patch.object(tool_call, "LOG_DIR", self.logs)
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.source = self.logs / "source.jsonl"
        self.old_messages = [
            {"role": "system", "content": "old-system"},
            {"role": "user", "content": "上次任务：验证 sum_to。"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_old", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "main.py", "content": "print(5050)\n"}),
                },
            }]},
            {"role": "tool", "tool_call_id": "call_old", "content": json.dumps({"ok": True, "path": "main.py"})},
        ]
        self.old_final = {"role": "assistant", "content": "上次结果是 5050。"}
        self.entries = [
            {"event": "run_start", "auto_read": True},
            {"event": "model_request", "step": 2, "payload": {"messages": self.old_messages, "model": "old-model", "max_tokens": 10}},
            {"event": "model_response", "step": 2, "response": {"choices": [{"finish_reason": "stop", "message": self.old_final}]}},
            {"event": "run_end", "reason": "stop"},
        ]
        self.save_entries(self.entries)
        self.final = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "已根据历史回答。"}}]}

    def save_entries(self, entries):
        self.source.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries), encoding="utf-8")

    def run_cli(self, arguments, replies):
        with patch.object(sys, "argv", ["tool_call.py", *arguments]):
            with patch.dict(os.environ, {"DEEPSEEK_MODEL": "current-model"}):
                with patch("tool_call.get_api_key", return_value="current-offline-key") as key:
                    with patch("tool_call.call_model", side_effect=replies) as model:
                        with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()) as output:
                            tool_call.main()
        return model, key, approval, output.getvalue()

    def test_loads_complete_history_including_last_assistant_answer(self):
        history = tool_call.load_history(str(self.source))
        self.assertEqual(history, [*self.old_messages[1:], self.old_final])

    def test_resume_uses_current_settings_and_does_not_replay_old_tools(self):
        original = self.source.read_bytes()
        with patch("tool_call.write_file") as writer, patch("tool_call.run_python") as runner:
            model, key, approval, output = self.run_cli(["--resume", str(self.source), "上次结果是什么？"], [self.final])
        key.assert_called_once_with()
        approval.assert_not_called()
        writer.assert_not_called()
        runner.assert_not_called()
        request, used_key = model.call_args.args
        self.assertEqual(used_key, "current-offline-key")
        self.assertEqual(request["model"], "current-model")
        self.assertEqual(request["max_tokens"], 2048)
        self.assertNotEqual(request["messages"][0]["content"], "old-system")
        self.assertEqual(request["messages"][1:-1], [*self.old_messages[1:], self.old_final])
        self.assertEqual(request["messages"][-1], {"role": "user", "content": "上次结果是什么？"})
        self.assertIn("载入 4 条历史消息", output)
        self.assertEqual(self.source.read_bytes(), original)
        new_log = next(self.logs.glob("run-*.jsonl"))
        entries = [json.loads(line) for line in new_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(entries[0]["resumed_from"], str(self.source.resolve()))
        self.assertFalse(entries[0]["auto_read"])
        self.assertEqual(entries[-1]["reason"], "stop")
        self.assertNotIn("current-offline-key", new_log.read_text(encoding="utf-8"))

    def test_preview_restores_history_without_network_tools_or_new_log(self):
        with patch("tool_call.write_file") as writer, patch("tool_call.run_python") as runner:
            model, key, approval, output = self.run_cli(["--preview", "--resume", str(self.source), "继续讨论"], [])
        payload, _ = json.JSONDecoder().raw_decode(output.split("\n", 1)[1])
        self.assertEqual(payload["messages"][1:-1], [*self.old_messages[1:], self.old_final])
        self.assertEqual(payload["messages"][-1]["content"], "继续讨论")
        for operation in (model, key, approval, writer, runner):
            operation.assert_not_called()
        self.assertEqual(list(self.logs.iterdir()), [self.source])

    def test_incomplete_or_malformed_history_is_rejected_before_credentials_or_api(self):
        cases = []
        for reason in ("user_denied", "max_model_calls", "abnormal_finish", "error", "cancelled"):
            entries = copy.deepcopy(self.entries)
            entries[-1]["reason"] = reason
            cases.append(entries)
        cases.append(self.entries[:-1])
        entries = copy.deepcopy(self.entries)
        entries[2]["step"] = 3
        cases.append(entries)
        entries = copy.deepcopy(self.entries)
        entries[1]["payload"]["messages"].pop()
        cases.append(entries)
        entries = copy.deepcopy(self.entries)
        entries[1]["payload"]["messages"][-1]["tool_call_id"] = "unknown"
        cases.append(entries)
        cases.append([])
        for index, entries in enumerate(cases):
            with self.subTest(case=index):
                self.save_entries(entries)
                with patch.object(sys, "argv", ["tool_call.py", "--resume", str(self.source), "继续"]):
                    with patch("tool_call.get_api_key") as key, patch("tool_call.call_model") as model, self.assertRaises(SystemExit):
                        tool_call.main()
                key.assert_not_called()
                model.assert_not_called()
                self.assertEqual(list(self.logs.iterdir()), [self.source])
        self.source.write_text("{broken json", encoding="utf-8")
        with self.assertRaises(ValueError):
            tool_call.load_history(str(self.source))

    def test_resume_requires_an_explicit_new_task(self):
        with patch.object(sys, "argv", ["tool_call.py", "--resume", str(self.source)]):
            with patch("tool_call.get_api_key") as key, patch("tool_call.call_model") as model:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                    tool_call.main()
        self.assertEqual(stopped.exception.code, 2)
        key.assert_not_called()
        model.assert_not_called()

    def test_resume_does_not_inherit_automatic_read_permission(self):
        reading = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{
            "id": "call_new", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"project.txt"}'},
        }]}}]}
        with patch("tool_call.read_file", return_value="项目资料") as reader, patch("tool_call.write_file") as writer:
            model, _, approval, _ = self.run_cli(["--resume", str(self.source), "再读一次项目资料"], [reading, self.final])
        self.assertEqual(model.call_count, 2)
        approval.assert_called_once()
        reader.assert_called_once_with("project.txt")
        writer.assert_not_called()
        entries = [json.loads(line) for line in next(self.logs.glob("run-*.jsonl")).read_text(encoding="utf-8").splitlines()]
        decision = next(entry for entry in entries if entry["event"] == "tool_approval")
        self.assertEqual(decision["source"], "user")
        self.assertEqual(decision["tool_call_id"], "call_new")


if __name__ == "__main__":
    unittest.main()
