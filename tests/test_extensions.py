import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call
from learn_agent import permissions, tools
from learn_agent.extensions import SKILL_MARKER, SKILL_POLICY, load_plugin, load_skill
from learn_agent.sessions import SessionStore


HAS_DEPS = all(importlib.util.find_spec(name) is not None for name in ("yaml", "jsonschema"))
NAME = "plugin_python_check_inspect_python"
EXAMPLE = Path(__file__).resolve().parents[1] / "extensions" / "python-check"


def reply(calls=None):
    message = {"role": "assistant", "content": "" if calls else "已核对源码结构。"}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}}


def call(path="main.py", call_id="inspect-1", **extra):
    return {"id": call_id, "type": "function", "function": {
        "name": NAME, "arguments": json.dumps({"path": path, **extra})}}


class ExtensionTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-extensions-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "中文 项目"
        self.project.mkdir()
        self.logs = self.root / "logs"
        self.extension = self.project / "extensions" / "python-check"
        shutil.copytree(EXAMPLE, self.extension)
        (self.project / "main.py").write_text(
            "def sum_to(n):\n    return sum(range(1, n + 1))\n\n"
            "raise RuntimeError('静态检查不可执行待检查源码')\n", encoding="utf-8")

    def run_cli(self, replies=(), args=(), answer="y", error=False):
        before = set(self.logs.glob("*.jsonl"))
        output = io.StringIO()
        argv = ["tool_call.py", "--workspace", str(self.project), *args, "核对 main.py，不修改文件。"]
        with patch.object(sys, "argv", argv), patch.object(tool_call, "LOG_DIR", self.logs), \
                patch("tool_call.get_api_key", return_value="offline-placeholder") as key, \
                patch("learn_agent.providers.call_model", side_effect=replies) as model, \
                patch("builtins.input", side_effect=answer if callable(answer) else None,
                      return_value=None if callable(answer) else answer) as approval, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            if error:
                with self.assertRaises(SystemExit) as caught:
                    tool_call.main()
                output.write(str(caught.exception))
            else:
                tool_call.main()
        paths = set(self.logs.glob("*.jsonl")) - before
        path = next(iter(paths)) if paths else None
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path else []
        return path, events, output.getvalue(), model, key, approval

    def preview(self, output):
        return json.loads(output[output.index("{\n"):output.index("\n预览结束")])


class ExtensionOptionalTests(ExtensionTestCase):
    def test_default_flow_loads_neither_extension_nor_optional_parser(self):
        with patch("tool_call.load_skill") as skill, patch("tool_call.load_plugin") as plugin:
            _, events, _, model, _, _ = self.run_cli([reply()])
        skill.assert_not_called()
        plugin.assert_not_called()
        self.assertIsNone(events[0]["skill_source"])
        self.assertIsNone(events[0]["plugin_source"])
        self.assertEqual(model.call_args.args[0]["tools"], tools.tool_schemas())

    def test_missing_dependencies_fail_before_key_network_or_logs(self):
        for module, flag in (("yaml", "--skill"), ("jsonschema", "--plugin")):
            with self.subTest(module=module), patch.dict(sys.modules, {module: None}):
                path, _, output, model, key, approval = self.run_cli(args=[flag, "extensions/python-check"], error=True)
            self.assertIsNone(path)
            self.assertIn("需要", output)
            key.assert_not_called()
            model.assert_not_called()
            approval.assert_not_called()


@unittest.skipUnless(HAS_DEPS, "使用 .venv Python 并安装 requirements.txt 后验证扩展")
class ExtensionTests(ExtensionTestCase):
    def test_skill_preview_includes_source_and_workflow_but_adds_no_tools(self):
        path, _, output, model, key, approval = self.run_cli(args=["--skill", "extensions/python-check", "--preview"])
        payload = self.preview(output)
        data = json.loads(payload["messages"][-1]["content"].split("\n", 1)[1])
        skill = data["active_skill"]
        self.assertEqual(skill["metadata"]["metadata"]["version"], "0.1.0")
        self.assertEqual(skill["sha256"], hashlib.sha256((self.extension / "SKILL.md").read_bytes()).hexdigest())
        self.assertEqual(data["current_user_task"], "核对 main.py，不修改文件。")
        self.assertIn("验收结果", skill["instructions"])
        self.assertEqual(payload["tools"], tools.tool_schemas())
        self.assertIn(SKILL_POLICY, payload["messages"][0]["content"])
        self.assertIsNone(path)
        self.assertFalse(self.logs.exists())
        model.assert_not_called()
        key.assert_not_called()
        approval.assert_not_called()

    def test_plugin_preview_and_load_do_not_execute_top_level_code(self):
        code = self.extension / "plugin.py"
        code.write_text("raise RuntimeError('不应执行插件顶层代码')\n", encoding="utf-8")
        _, _, output, model, key, approval = self.run_cli(args=["--plugin", "extensions/python-check", "--preview"])
        payload = self.preview(output)
        self.assertIn(NAME, [tool["function"]["name"] for tool in payload["tools"]])
        self.assertNotIn(SKILL_MARKER, payload["messages"][-1]["content"])
        model.assert_not_called()
        key.assert_not_called()
        approval.assert_not_called()

    def test_skill_and_project_rules_keep_current_task_separate(self):
        (self.project / "rules.md").write_text("保持现有测试。", encoding="utf-8")
        _, _, output, _, _, _ = self.run_cli(args=["--skill", "extensions/python-check", "--instructions", "rules.md", "--preview"])
        payload = self.preview(output)
        data = json.loads(payload["messages"][-1]["content"].split("\n", 1)[1])
        self.assertEqual(data["project_rules"]["text"], "保持现有测试。")
        self.assertEqual(data["current_user_task"], "核对 main.py，不修改文件。")

    def test_invalid_skill_sources_fail_before_api(self):
        valid = (self.extension / "SKILL.md").read_bytes()
        invalid = [b"no frontmatter", b"---\nname: [\n---\nbody", b"\xff", b"\0", b"x" * 8193,
                   valid.replace(b"name: python-check", b"name: wrong"),
                   valid.replace(b"description:", b"unsupported: true\ndescription:"),
                   valid.replace(b'version: "0.1.0"', b"version: 1")]
        for raw in invalid:
            with self.subTest(raw=raw[:40]):
                (self.extension / "SKILL.md").write_bytes(raw)
                path, _, _, model, key, _ = self.run_cli(args=["--skill", "extensions/python-check"], error=True)
                self.assertIsNone(path)
                model.assert_not_called()
                key.assert_not_called()

    def test_utf8_bom_and_allowed_tools_metadata_do_not_grant_permissions(self):
        skill = self.extension / "SKILL.md"
        text = skill.read_text(encoding="utf-8").replace("description:", "allowed-tools: run_python\ndescription:")
        skill.write_text(text, encoding="utf-8-sig")
        with permissions.use_workspace(str(self.project)):
            loaded = load_skill("extensions/python-check")
        self.assertEqual(loaded["metadata"]["allowed-tools"], "run_python")
        _, events, _, _, _, approval = self.run_cli([reply([call()])], ["--skill", "extensions/python-check", "--plugin", "extensions/python-check", "--auto-read"], answer="n")
        approval.assert_called_once()
        self.assertEqual(events[-1]["reason"], "user_denied")

    def test_extension_cannot_be_loaded_from_outside_workspace(self):
        shutil.copytree(EXAMPLE, self.root / "python-check")
        for flag in ("--skill", "--plugin"):
            with self.subTest(flag=flag):
                _, _, _, model, key, _ = self.run_cli(args=[flag, "../python-check"], error=True)
                model.assert_not_called()
                key.assert_not_called()

    def test_invalid_manifest_or_source_fails_before_api(self):
        path = self.extension / "plugin.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        cases = [{}, {**original, "version": "bad"}, {**original, "tools": original["tools"] * 2},
                 {**original, "tools": [{**original["tools"][0], "parameters": {"type": "object"}}]},
                 *[{**original, "tools": [{**original["tools"][0], "parameters": {
                     "type": "object", "properties": {"path": {keyword: "https://invalid.example/schema"}}, "additionalProperties": False}}]}
                   for keyword in ("$ref", "$dynamicRef")]]
        for manifest in cases:
            with self.subTest(manifest=manifest):
                path.write_text(json.dumps(manifest), encoding="utf-8")
                _, _, _, model, key, _ = self.run_cli(args=["--plugin", "extensions/python-check"], error=True)
                model.assert_not_called()
                key.assert_not_called()
        path.write_text(json.dumps(original), encoding="utf-8")
        (self.extension / "plugin.py").write_text("def invalid(\n", encoding="utf-8")
        _, _, output, model, key, _ = self.run_cli(args=["--plugin", "extensions/python-check"], error=True)
        self.assertIn("语法错误", output)
        model.assert_not_called()
        key.assert_not_called()

    def test_approved_plugin_inspects_ast_without_executing_target_and_saves_provenance(self):
        registry = dict(tools.TOOL_REGISTRY)
        _, events, _, _, _, approval = self.run_cli([reply([call()]), reply()],
            ["--skill", "extensions/python-check", "--plugin", "extensions/python-check", "--auto-read", "--stream"])
        approval.assert_called_once()
        result = next(e["result"] for e in events if e["event"] == "tool_result")
        self.assertTrue(result["ok"])
        self.assertEqual(result["tool"], NAME)
        self.assertEqual(result["plugin_version"], "0.1.0")
        self.assertEqual(result["value"]["definitions"], [{"name": "sum_to", "kind": "FunctionDef", "line": 1}])
        self.assertFalse(result["value"]["source_executed"])
        self.assertEqual(events[0]["plugin_source"]["code"]["sha256"], hashlib.sha256((self.extension / "plugin.py").read_bytes()).hexdigest())
        self.assertNotIn("instructions", events[0]["skill_source"])
        self.assertEqual(events[-1]["reason"], "stop")
        self.assertEqual(events[-1]["summary"]["usage"]["total_tokens"], 200)
        _, history = SessionStore(self.logs / "sessions.sqlite3").restore(events[0]["session_id"], self.project)
        self.assertTrue(any(NAME in (m.get("content") or "") for m in history if m["role"] == "tool"))
        self.assertEqual(tools.TOOL_REGISTRY, registry)

    def test_rejection_and_explicit_denial_do_not_execute_plugin_imports(self):
        (self.extension / "plugin.py").write_text("raise RuntimeError('必须在确认后才执行')\n", encoding="utf-8")
        for extra, answer, reason in (([], "n", "user_denied"), (["--deny-tool", NAME], "y", "policy_denied")):
            with self.subTest(reason=reason):
                _, events, _, _, _, approval = self.run_cli([reply([call()])], ["--plugin", "extensions/python-check", "--auto-read", *extra], answer)
                self.assertEqual(approval.call_count, 1 if answer == "n" else 0)
                self.assertFalse(any(e["event"] in ("tool_started", "tool_result") for e in events))
                self.assertEqual(events[-1]["reason"], reason)

    def test_invalid_second_arguments_prevent_entire_batch_execution(self):
        for bad in (True, "", 42):
            with self.subTest(path=bad):
                _, events, _, _, _, approval = self.run_cli([reply([call(), call(bad, "bad")])],
                    ["--plugin", "extensions/python-check"], error=True)
                approval.assert_not_called()
                self.assertFalse(any(e["event"] == "tool_result" for e in events))
                self.assertEqual(events[-1]["reason"], "error")

    def test_changed_code_or_manifest_during_approval_is_not_executed(self):
        for filename in ("plugin.py", "plugin.json"):
            path = self.extension / filename
            original = path.read_bytes()
            def change(_):
                path.write_bytes(original + b"\n ")
                return "y"
            with self.subTest(filename=filename):
                _, events, _, _, _, _ = self.run_cli([reply([call()]), reply()], ["--plugin", "extensions/python-check"], change)
                result = next(e["result"] for e in events if e["event"] == "tool_result")
                self.assertFalse(result["ok"])
                self.assertIn("发生变化", result["error"]["message"])
            path.write_bytes(original)

    def test_runtime_errors_and_invalid_results_return_failure_to_model(self):
        bodies = ["raise RuntimeError('插件失败')", "def wrong(): pass",
                  "def execute(*args): return {1, 2}", "def execute(*args): return float('nan')",
                  "def execute(*args): return 'x' * 32769"]
        for code in bodies:
            with self.subTest(code=code):
                (self.extension / "plugin.py").write_text(code, encoding="utf-8")
                _, events, _, model, _, _ = self.run_cli([reply([call()]), reply()], ["--plugin", "extensions/python-check"])
                result = next(e["result"] for e in events if e["event"] == "tool_result")
                self.assertFalse(result["ok"])
                self.assertIn("error", result)
                self.assertEqual(model.call_count, 2)

    def test_plugin_value_cannot_override_host_result_fields(self):
        (self.extension / "plugin.py").write_text("def execute(*args): return {'tool': 'fake', 'ok': False}\n", encoding="utf-8")
        _, events, _, _, _, _ = self.run_cli([reply([call()]), reply()], ["--plugin", "extensions/python-check"])
        result = next(e["result"] for e in events if e["event"] == "tool_result")
        self.assertEqual((result["tool"], result["ok"]), (NAME, True))
        self.assertEqual(result["value"], {"tool": "fake", "ok": False})

    def test_resume_keeps_history_but_does_not_reload_skill_or_plugin(self):
        _, first, _, _, _, _ = self.run_cli([reply([call()]), reply()], ["--skill", "extensions/python-check", "--plugin", "extensions/python-check"])
        (self.extension / "SKILL.md").unlink()
        (self.extension / "plugin.py").unlink()
        _, events, _, model, _, approval = self.run_cli([reply()], ["--session", first[0]["session_id"]])
        payload = model.call_args.args[0]
        self.assertNotIn(NAME, [t["function"]["name"] for t in payload["tools"]])
        self.assertIn(SKILL_POLICY, payload["messages"][0]["content"])
        self.assertTrue(any(SKILL_MARKER in (m.get("content") or "") for m in payload["messages"][1:]))
        self.assertIsNone(events[0]["skill_source"])
        self.assertIsNone(events[0]["plugin_source"])
        self.assertFalse(any(e["event"] == "tool_result" for e in events))
        approval.assert_not_called()

    def test_budget_pauses_before_plugin_execution(self):
        _, events, _, _, _, approval = self.run_cli([reply([call()])], ["--plugin", "extensions/python-check", "--max-total-tokens", "1"])
        approval.assert_not_called()
        self.assertFalse(any(e["event"] == "tool_result" for e in events))
        self.assertEqual(events[-1]["reason"], "max_total_tokens")

    def test_unknown_denied_plugin_name_fails_before_key_model_logs(self):
        _, _, _, model, key, approval = self.run_cli(args=["--plugin", "extensions/python-check", "--deny-tool", NAME + "_typo"], error=True)
        model.assert_not_called()
        key.assert_not_called()
        approval.assert_not_called()
        self.assertFalse(self.logs.exists())


if __name__ == "__main__":
    unittest.main()
