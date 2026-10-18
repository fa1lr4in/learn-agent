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
from learn_agent import loop, permissions, tools


class ToolCallTests(unittest.TestCase):
    def setUp(self):
        self.payload = {"messages": [{"role": "system", "content": "test"}, {"role": "user", "content": "read"}]}
        self.request = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_test", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": "project.txt"})
            }}],
        }
        self.response = {"choices": [{"message": self.request, "finish_reason": "tool_calls"}]}

    def test_tool_result_is_paired_and_sent_with_history(self):
        final = {"choices": [{"message": {"content": "test answer"}, "finish_reason": "stop"}]}
        with patch("learn_agent.loop.call_model", side_effect=[self.response, final]) as model:
            with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
                loop.run_agent(self.payload, "offline-placeholder")
        self.assertEqual(model.call_count, 2)
        followup = model.call_args_list[1].args[0]
        self.assertEqual([message["role"] for message in followup["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(followup["messages"][-1]["tool_call_id"], "call_test")
        result = json.loads(followup["messages"][-1]["content"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "project.txt")
        self.assertEqual(result["content"], tools.read_file("project.txt"))
        self.assertEqual(followup["tool_choice"], "auto")
        self.assertEqual(len(self.payload["messages"]), 2)

    def test_multiple_rounds_keep_results_and_allow_more_tools(self):
        second = copy.deepcopy(self.response)
        second_call = second["choices"][0]["message"]["tool_calls"][0]
        second_call["id"] = "call_next"
        second_call["function"]["arguments"] = json.dumps({"path": "next_step.txt"})
        final = {"choices": [{"message": {"content": "汇总完成。"}, "finish_reason": "stop"}]}
        with patch("learn_agent.loop.call_model", side_effect=[self.response, second, final]) as model:
            with patch("learn_agent.tools.read_file", side_effect=["项目资料", "下一步任务"]) as reader:
                with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                    loop.run_agent(self.payload, "offline-placeholder")
        self.assertEqual(model.call_count, 3)
        self.assertEqual(approval.call_count, 2)
        self.assertEqual([call.args[0] for call in reader.call_args_list], ["project.txt", "next_step.txt"])
        requests = [call.args[0] for call in model.call_args_list]
        self.assertEqual([len(request["messages"]) for request in requests], [2, 4, 6])
        self.assertTrue(all(request["tool_choice"] == "auto" for request in requests))
        history = requests[-1]["messages"]
        self.assertEqual([message["role"] for message in history], ["system", "user", "assistant", "tool", "assistant", "tool"])
        self.assertEqual([history[index]["tool_call_id"] for index in (3, 5)], ["call_test", "call_next"])
        results = [json.loads(history[index]["content"]) for index in (3, 5)]
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual([result["content"] for result in results], ["项目资料", "下一步任务"])
        self.assertEqual(len(self.payload["messages"]), 2)

    def test_list_files_returns_visible_files_and_subdirectories(self):
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            workspace = Path(directory)
            self.assertEqual(workspace.resolve().parent, Path(tempfile.gettempdir()).resolve())
            (workspace / "project.txt").write_text("项目资料", encoding="utf-8")
            (workspace / "report.md").write_text("报告", encoding="utf-8")
            (workspace / "main.py").write_text("print(5050)\n", encoding="utf-8")
            (workspace / "test_main.py").write_text("验收脚本", encoding="utf-8")
            (workspace / ".env").write_text("不允许读取", encoding="utf-8")
            (workspace / "src").mkdir()
            with patch.object(permissions, "WORKSPACE", workspace):
                self.assertEqual(tools.list_files(), ["main.py", "project.txt", "report.md", "src/", "test_main.py"])

    def test_read_file_can_inspect_generated_report_and_python_sources(self):
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            workspace = Path(directory)
            self.assertEqual(workspace.resolve().parent, Path(tempfile.gettempdir()).resolve())
            for name in ("report.md", "main.py", "test_main.py"):
                with self.subTest(path=name):
                    content = f"{name} 的完整练习内容\n"
                    (workspace / name).write_text(content, encoding="utf-8")
                    with patch.object(permissions, "WORKSPACE", workspace):
                        self.assertEqual(tools.read_file(name), content)

    def test_list_then_read_dispatches_to_correct_functions(self):
        listing = copy.deepcopy(self.response)
        listing_call = listing["choices"][0]["message"]["tool_calls"][0]
        listing_call["id"] = "call_list"
        listing_call["function"] = {"name": "list_files", "arguments": "{}"}
        final = {"choices": [{"message": {"content": "项目叫小灯。"}, "finish_reason": "stop"}]}
        with patch("learn_agent.loop.call_model", side_effect=[listing, self.response, final]) as model:
            with patch("learn_agent.tools.list_files", return_value=["project.txt"]) as lister:
                with patch("learn_agent.tools.read_file", return_value="项目名称：小灯") as reader:
                    with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                        loop.run_agent(self.payload, "offline-placeholder")
        lister.assert_called_once_with(".")
        reader.assert_called_once_with("project.txt")
        self.assertEqual(approval.call_count, 2)
        self.assertEqual(model.call_count, 3)
        history = model.call_args_list[-1].args[0]["messages"]
        self.assertEqual([history[index]["tool_call_id"] for index in (3, 5)], ["call_list", "call_test"])
        listed, read = [json.loads(history[index]["content"]) for index in (3, 5)]
        self.assertEqual(listed, {"ok": True, "tool": "list_files", "scope": "visible_workspace_directory", "path": ".", "files": ["project.txt"]})
        self.assertEqual(read["tool"], "read_file")
        self.assertEqual(read["content"], "项目名称：小灯")

    def test_list_then_batched_reads_returns_both_results_in_one_followup(self):
        listing = copy.deepcopy(self.response)
        listing["choices"][0]["message"]["tool_calls"][0] = {
            "id": "call_list", "type": "function", "function": {"name": "list_files", "arguments": "{}"},
        }
        second = copy.deepcopy(self.response)
        next_call = copy.deepcopy(self.request["tool_calls"][0])
        next_call["id"] = "call_next"
        next_call["function"]["arguments"] = json.dumps({"path": "next_step.txt"})
        second["choices"][0]["message"]["tool_calls"].append(next_call)
        final = {"choices": [{"message": {"content": "汇总完成。"}, "finish_reason": "stop"}]}
        log_file = io.StringIO()
        with patch("learn_agent.loop.call_model", side_effect=[listing, second, final]) as model:
            with patch("learn_agent.tools.list_files", return_value=["project.txt", "next_step.txt"]):
                with patch("learn_agent.tools.read_file", side_effect=["项目资料", "下一步任务"]) as reader:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                        reason = loop.run_agent(self.payload, "offline-placeholder", log_file, auto_read=True)
        self.assertEqual(reason, "stop")
        self.assertEqual(model.call_count, 3)
        approval.assert_not_called()
        self.assertEqual([call.args[0] for call in reader.call_args_list], ["project.txt", "next_step.txt"])
        history = model.call_args_list[-1].args[0]["messages"]
        self.assertEqual([entry["role"] for entry in history], ["system", "user", "assistant", "tool", "assistant", "tool", "tool"])
        self.assertEqual(history[4]["tool_calls"], second["choices"][0]["message"]["tool_calls"])
        self.assertEqual([entry["tool_call_id"] for entry in history[5:]], ["call_test", "call_next"])
        self.assertEqual([json.loads(entry["content"])["content"] for entry in history[5:]], ["项目资料", "下一步任务"])
        entries = [json.loads(line) for line in log_file.getvalue().splitlines()]
        decisions = [entry for entry in entries if entry["event"] == "tool_approval" and entry["step"] == 2]
        self.assertEqual([entry["tool_call_id"] for entry in decisions], ["call_test", "call_next"])
        self.assertTrue(all(entry["source"] == "auto_read" for entry in decisions))
        self.assertEqual(len(self.payload["messages"]), 2)

    def test_writer_creates_and_overwrites_utf8_report(self):
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            workspace = Path(directory)
            self.assertEqual(workspace.resolve().parent, Path(tempfile.gettempdir()).resolve())
            with patch.object(permissions, "WORKSPACE", workspace):
                for content in ("# 项目报告\n项目名称：小灯\n实验编号：7391", "# 新报告\n更新后的内容"):
                    self.assertEqual(tools.write_file("report.md", content), len(content))
                    self.assertEqual((workspace / "report.md").read_text(encoding="utf-8"), content)
                self.assertEqual([path.name for path in workspace.iterdir()], ["report.md"])

    def test_writer_rejects_other_paths_and_empty_content(self):
        for path, content in [("../chat.py", "报告"), ("project.txt", "报告"), ("test_main.py", "报告"), ("report.md", "  "), ("report.md", 123)]:
            with self.subTest(path=path, content=content):
                with patch.object(Path, "write_text") as writer, self.assertRaises(ValueError):
                    tools.write_file(path, content)
                writer.assert_not_called()

    def test_writer_rejects_symbolic_link(self):
        with patch.object(Path, "is_symlink", return_value=True):
            with patch.object(Path, "write_text") as writer, self.assertRaises(ValueError):
                tools.write_file("report.md", "报告")
        writer.assert_not_called()

    def test_full_sequence_lists_reads_and_writes_within_limit(self):
        content = "# 项目报告\n项目名称：小灯\n实验编号：7391\n下一步任务：把单次工具调用扩展为 Agent 循环。"
        steps = [
            ("list_files", {}),
            ("read_file", {"path": "project.txt"}),
            ("read_file", {"path": "next_step.txt"}),
            ("write_file", {"path": "report.md", "content": content}),
        ]
        responses = []
        for index, (name, arguments) in enumerate(steps):
            response = copy.deepcopy(self.response)
            response["choices"][0]["message"]["tool_calls"][0] = {
                "id": f"call_{index}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
            responses.append(response)
        responses.append({"choices": [{"message": {"content": "报告已保存。"}, "finish_reason": "stop"}]})
        sources = {name: tools.read_file(name) for name in ("project.txt", "next_step.txt")}
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            workspace = Path(directory)
            self.assertEqual(workspace.resolve().parent, Path(tempfile.gettempdir()).resolve())
            for name, source in sources.items():
                (workspace / name).write_text(source, encoding="utf-8")
            with patch.object(permissions, "WORKSPACE", workspace):
                with patch("learn_agent.loop.call_model", side_effect=responses) as model:
                    with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()) as output:
                        loop.run_agent(self.payload, "offline-placeholder")
            self.assertEqual((workspace / "report.md").read_text(encoding="utf-8"), content)
            for name, source in sources.items():
                self.assertEqual((workspace / name).read_text(encoding="utf-8"), source)
        self.assertEqual(model.call_count, 5)
        self.assertEqual(approval.call_count, 4)
        self.assertIn("创建 report.md", approval.call_args.args[0])
        history = model.call_args_list[-1].args[0]["messages"]
        self.assertEqual([history[index]["tool_call_id"] for index in (3, 5, 7, 9)], [f"call_{index}" for index in range(4)])
        result = json.loads(history[-1]["content"])
        self.assertEqual(result, {"ok": True, "tool": "write_file", "path": "report.md", "characters_written": len(content)})
        self.assertIn("[助手最终回答]\n报告已保存。", output.getvalue())
        self.assertNotIn("任务未完成", output.getvalue())

    def test_auto_read_full_run_only_asks_before_writing(self):
        content = "# 报告\n项目名称：小灯\n实验编号：7391"
        steps = [
            ("list_files", {}),
            ("read_file", {"path": "project.txt"}),
            ("read_file", {"path": "next_step.txt"}),
            ("write_file", {"path": "report.md", "content": content}),
        ]
        responses = []
        for index, (name, arguments) in enumerate(steps):
            response = copy.deepcopy(self.response)
            response["choices"][0]["message"]["tool_calls"][0] = {
                "id": f"call_{index}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
            responses.append(response)
        responses.append({"choices": [{"message": {"content": "已保存。"}, "finish_reason": "stop"}]})
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            workspace = Path(directory)
            self.assertEqual(workspace.resolve().parent, Path(tempfile.gettempdir()).resolve())
            (workspace / "project.txt").write_text("项目名称：小灯\n实验编号：7391", encoding="utf-8")
            (workspace / "next_step.txt").write_text("下一步任务：学习 Agent 循环。", encoding="utf-8")
            log_dir = workspace / "logs"
            with patch.object(permissions, "WORKSPACE", workspace), patch.object(tool_call, "LOG_DIR", log_dir):
                with patch.object(sys, "argv", ["tool_call.py", "--auto-read"]), patch("tool_call.get_api_key", return_value="offline-placeholder"):
                    with patch("learn_agent.loop.call_model", side_effect=responses) as model:
                        with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                            tool_call.main()
            self.assertEqual((workspace / "report.md").read_text(encoding="utf-8"), content)
            entries = [json.loads(line) for line in next(log_dir.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(model.call_count, 5)
        approval.assert_called_once()
        self.assertIn("创建 report.md", approval.call_args.args[0])
        decisions = [entry for entry in entries if entry["event"] == "tool_approval"]
        self.assertEqual([entry["source"] for entry in decisions], ["auto_read", "auto_read", "auto_read", "user"])
        self.assertTrue(all(entry["approved"] for entry in decisions))
        self.assertTrue(entries[0]["auto_read"])
        self.assertEqual(entries[-1]["reason"], "stop")
        self.assertTrue(all("auto_read" not in entry["payload"] for entry in entries if entry["event"] == "model_request"))

    def test_auto_read_does_not_authorize_report_overwrite(self):
        self.request["tool_calls"][0]["function"] = {
            "name": "write_file", "arguments": json.dumps({"path": "report.md", "content": "新报告"}),
        }
        log_file = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            workspace = Path(directory)
            self.assertEqual(workspace.resolve().parent, Path(tempfile.gettempdir()).resolve())
            (workspace / "report.md").write_text("旧报告", encoding="utf-8")
            with patch.object(permissions, "WORKSPACE", workspace), patch("learn_agent.loop.call_model", return_value=self.response) as model:
                with patch("builtins.input", return_value="n") as approval, contextlib.redirect_stdout(io.StringIO()) as output:
                    reason = loop.run_agent(self.payload, "offline-placeholder", log_file, auto_read=True)
            self.assertEqual((workspace / "report.md").read_text(encoding="utf-8"), "旧报告")
        model.assert_called_once()
        approval.assert_called_once()
        self.assertIn("覆盖现有 report.md", approval.call_args.args[0])
        self.assertIn("新报告", output.getvalue())
        self.assertEqual(reason, "user_denied")
        decision = json.loads(log_file.getvalue().splitlines()[-1])
        self.assertEqual(decision["source"], "user")
        self.assertFalse(decision["approved"])

    def test_auto_read_rejects_invalid_requests_before_authorization(self):
        invalid = [
            ("shell", {}),
            ("read_file", {"path": "../chat.py"}),
            ("list_files", {"path": ".."}),
            ("write_file", {"path": "project.txt", "content": "改写资料"}),
            ("write_file", {"path": "report.md", "content": "报告", "auto_read": True}),
        ]
        for name, arguments in invalid:
            with self.subTest(name=name, arguments=arguments):
                self.request["tool_calls"][0]["function"] = {"name": name, "arguments": json.dumps(arguments)}
                with patch("learn_agent.loop.call_model", return_value=self.response), patch("builtins.input") as approval:
                    with patch("learn_agent.tools.read_file") as reader, patch("learn_agent.tools.list_files") as lister, patch("learn_agent.tools.write_file") as writer:
                        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                            loop.run_agent(self.payload, "offline-placeholder", auto_read=True)
                approval.assert_not_called()
                reader.assert_not_called()
                lister.assert_not_called()
                writer.assert_not_called()

    def test_auto_read_returns_file_error_to_model_without_confirmation(self):
        final = {"choices": [{"message": {"content": "读取失败。"}, "finish_reason": "stop"}]}
        with patch("learn_agent.loop.call_model", side_effect=[self.response, final]) as model:
            with patch("learn_agent.tools.read_file", side_effect=FileNotFoundError()), patch("builtins.input") as approval:
                with contextlib.redirect_stdout(io.StringIO()):
                    reason = loop.run_agent(self.payload, "offline-placeholder", auto_read=True)
        self.assertEqual(model.call_count, 2)
        approval.assert_not_called()
        self.assertEqual(reason, "stop")
        result = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "FileNotFoundError")

    def test_auto_read_preview_does_not_read_write_or_log(self):
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            self.assertEqual(Path(directory).resolve().parent, Path(tempfile.gettempdir()).resolve())
            log_dir = Path(directory) / "logs"
            with patch.object(tool_call, "LOG_DIR", log_dir), patch.object(sys, "argv", ["tool_call.py", "--auto-read", "--preview"]):
                with patch("tool_call.get_api_key") as key, patch("learn_agent.loop.call_model") as model:
                    with patch("learn_agent.tools.read_file") as reader, patch("learn_agent.tools.list_files") as lister, patch("learn_agent.tools.write_file") as writer:
                        with contextlib.redirect_stdout(io.StringIO()):
                            tool_call.main()
            self.assertFalse(log_dir.exists())
        for operation in (key, model, reader, lister, writer):
            operation.assert_not_called()

    def test_overwrite_requires_approval_and_shows_content(self):
        content = "# 新报告\n项目名称：小灯"
        self.request["tool_calls"][0]["function"] = {
            "name": "write_file", "arguments": json.dumps({"path": "report.md", "content": content}),
        }
        final = {"choices": [{"message": {"content": "已保存。"}, "finish_reason": "stop"}]}
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            workspace = Path(directory)
            self.assertEqual(workspace.resolve().parent, Path(tempfile.gettempdir()).resolve())
            for answer in ("n", "y"):
                with self.subTest(answer=answer):
                    (workspace / "report.md").write_text("旧报告", encoding="utf-8")
                    with patch.object(permissions, "WORKSPACE", workspace):
                        with patch("learn_agent.loop.call_model", side_effect=[self.response, final]) as model:
                            with patch("builtins.input", return_value=answer) as approval, contextlib.redirect_stdout(io.StringIO()) as output:
                                loop.run_agent(self.payload, "offline-placeholder")
                    self.assertEqual((workspace / "report.md").read_text(encoding="utf-8"), content if answer == "y" else "旧报告")
                    self.assertEqual(model.call_count, 2 if answer == "y" else 1)
                    approval.assert_called_once()
                    self.assertIn("覆盖现有 report.md", approval.call_args.args[0])
                    self.assertIn(content, output.getvalue())

    def test_write_arguments_are_validated_before_confirmation(self):
        invalid = [
            {"path": "../chat.py", "content": "报告"},
            {"path": "project.txt", "content": "报告"},
            {"path": "test_main.py", "content": "报告"},
            {"path": "report.md"},
            {"path": "report.md", "content": "报告", "extra": True},
            {"path": "report.md", "content": "\n "},
            {"path": "report.md", "content": 123},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.request["tool_calls"][0]["function"] = {"name": "write_file", "arguments": json.dumps(arguments)}
                with patch("learn_agent.loop.call_model", return_value=self.response), patch("learn_agent.tools.write_file") as writer:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                        loop.run_agent(self.payload, "offline-placeholder")
                writer.assert_not_called()
                approval.assert_not_called()

    def test_write_failure_is_returned_to_model(self):
        self.request["tool_calls"][0]["function"] = {
            "name": "write_file", "arguments": json.dumps({"path": "report.md", "content": "报告"}),
        }
        final = {"choices": [{"message": {"content": "保存失败。"}, "finish_reason": "stop"}]}
        with patch("learn_agent.loop.call_model", side_effect=[self.response, final]) as model:
            with patch("learn_agent.tools.write_file", side_effect=PermissionError("private-local-path")):
                with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
                    loop.run_agent(self.payload, "offline-placeholder")
        result = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
        self.assertEqual(result["path"], "report.md")
        self.assertEqual(result["tool"], "write_file")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "PermissionError")
        self.assertNotIn("characters_written", result)
        self.assertNotIn("private-local-path", json.dumps(result))

    def test_list_files_rejects_arguments_before_confirmation(self):
        self.request["tool_calls"][0]["function"] = {"name": "list_files", "arguments": '{"path":".."}'}
        with patch("learn_agent.loop.call_model", return_value=self.response), patch("learn_agent.tools.list_files") as lister:
            with patch("builtins.input") as approval:
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                    loop.run_agent(self.payload, "offline-placeholder")
        lister.assert_not_called()
        approval.assert_not_called()

    def test_list_failure_is_returned_as_tool_result(self):
        self.request["tool_calls"][0]["function"] = {"name": "list_files", "arguments": "{}"}
        final = {"choices": [{"message": {"content": "无法列出文件。"}, "finish_reason": "stop"}]}
        with patch("learn_agent.loop.call_model", side_effect=[self.response, final]) as model:
            with patch("learn_agent.tools.list_files", side_effect=PermissionError()):
                with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
                    loop.run_agent(self.payload, "offline-placeholder")
        result = json.loads(model.call_args_list[-1].args[0]["messages"][-1]["content"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["tool"], "list_files")
        self.assertEqual(result["error"]["type"], "PermissionError")

    def test_read_errors_are_returned_to_model(self):
        errors = [
            FileNotFoundError("private-local-path"),
            PermissionError("private-local-path"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"),
        ]
        final = {"choices": [{"message": {"content": "读取失败。"}, "finish_reason": "stop"}]}
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with patch("learn_agent.loop.call_model", side_effect=[self.response, final]) as model:
                    with patch("learn_agent.tools.read_file", side_effect=error):
                        with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
                            loop.run_agent(self.payload, "offline-placeholder")
                self.assertEqual(model.call_count, 2)
                message = model.call_args_list[1].args[0]["messages"][-1]
                self.assertEqual(message["role"], "tool")
                self.assertEqual(message["tool_call_id"], "call_test")
                result = json.loads(message["content"])
                self.assertFalse(result["ok"])
                self.assertEqual(result["path"], "project.txt")
                self.assertEqual(result["error"]["type"], type(error).__name__)
                self.assertNotIn("private-local-path", message["content"])
                self.assertNotIn("content", result)

    def test_model_can_choose_another_file_after_read_failure(self):
        first = copy.deepcopy(self.response)
        first["choices"][0]["message"]["tool_calls"][0]["id"] = "call_missing"
        first["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps({"path": "next_step.txt"})
        final = {"choices": [{"message": {"content": "已根据项目资料回答。"}, "finish_reason": "stop"}]}
        with patch("learn_agent.loop.call_model", side_effect=[first, self.response, final]) as model:
            with patch("learn_agent.tools.read_file", side_effect=[FileNotFoundError(), "项目名称：小灯"]) as reader:
                with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                    loop.run_agent(self.payload, "offline-placeholder")
        self.assertEqual(model.call_count, 3)
        self.assertEqual(approval.call_count, 2)
        self.assertEqual([call.args[0] for call in reader.call_args_list], ["next_step.txt", "project.txt"])
        history = model.call_args_list[-1].args[0]["messages"]
        failed, succeeded = [json.loads(history[index]["content"]) for index in (3, 5)]
        self.assertFalse(failed["ok"])
        self.assertTrue(succeeded["ok"])
        self.assertEqual(succeeded["content"], "项目名称：小灯")

    def test_repeated_read_errors_still_respect_request_limit(self):
        with patch("learn_agent.loop.call_model", return_value=self.response) as model:
            with patch("learn_agent.tools.read_file", side_effect=FileNotFoundError()) as reader:
                with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()) as output:
                    loop.run_agent(self.payload, "offline-placeholder")
        self.assertEqual(model.call_count, loop.MAX_MODEL_CALLS)
        self.assertEqual(reader.call_count, loop.MAX_MODEL_CALLS - 1)
        self.assertIn("任务未完成", output.getvalue())

    def test_limit_stops_before_executing_another_tool(self):
        with patch.object(loop, "MAX_MODEL_CALLS", 3):
            with patch("learn_agent.loop.call_model", return_value=self.response) as model:
                with patch("learn_agent.tools.read_file", return_value="项目资料") as reader:
                    with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()) as output:
                        loop.run_agent(self.payload, "offline-placeholder")
        self.assertEqual(model.call_count, 3)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(approval.call_count, 2)
        self.assertIn("已达到最多 3 次模型请求，任务未完成", output.getvalue())

    def test_refusal_does_not_read_or_call_model_again(self):
        for name, arguments in [("read_file", '{"path":"project.txt"}'), ("list_files", "{}")]:
            with self.subTest(name=name):
                response = copy.deepcopy(self.response)
                response["choices"][0]["message"]["tool_calls"][0]["function"] = {"name": name, "arguments": arguments}
                with patch("learn_agent.loop.call_model", return_value=response) as model:
                    with patch("learn_agent.tools.read_file") as reader, patch("learn_agent.tools.list_files") as lister:
                        with patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
                            loop.run_agent(self.payload, "offline-placeholder")
                model.assert_called_once()
                reader.assert_not_called()
                lister.assert_not_called()

    def test_text_reply_does_not_execute_tool_or_ask_for_confirmation(self):
        response = {"choices": [{"message": {"role": "assistant", "content": "你好。"}, "finish_reason": "stop"}]}
        with patch("learn_agent.loop.call_model", return_value=response) as model, patch("learn_agent.tools.read_file") as reader:
            with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                loop.run_agent(self.payload, "offline-placeholder")
        model.assert_called_once()
        reader.assert_not_called()
        approval.assert_not_called()

    def test_preview_shows_question_without_key_network_or_file_access(self):
        for arguments, expected in [
            ([], "请先列出可用的练习文件，再读取项目资料和其中提到的任务文件，汇总项目名称、实验编号和下一步任务，用 write_file 将 Markdown 报告保存到 report.md，最后说明保存结果。"),
            (["请用一句话介绍自己。"], "请用一句话介绍自己。"),
        ]:
            with self.subTest(arguments=arguments):
                with patch.object(sys, "argv", ["tool_call.py", "--preview", *arguments]):
                    with patch("tool_call.get_api_key") as key, patch("learn_agent.loop.call_model") as model:
                        with patch("learn_agent.tools.read_file") as reader, patch("learn_agent.tools.list_files") as lister, patch("learn_agent.tools.write_file") as writer, contextlib.redirect_stdout(io.StringIO()) as output:
                            tool_call.main()
                payload, _ = json.JSONDecoder().raw_decode(output.getvalue().split("\n", 1)[1])
                self.assertEqual(payload["messages"][-1], {"role": "user", "content": expected})
                key.assert_not_called()
                model.assert_not_called()
                reader.assert_not_called()
                lister.assert_not_called()
                writer.assert_not_called()

    def test_unapproved_path_is_rejected_before_confirmation(self):
        self.request["tool_calls"][0]["function"]["arguments"] = json.dumps({"path": "../chat.py"})
        with patch("learn_agent.loop.call_model", return_value=self.response), patch("builtins.input") as approval:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                loop.run_agent(self.payload, "offline-placeholder")
        approval.assert_not_called()

    def test_unknown_tool_and_invalid_arguments_are_rejected(self):
        for name, arguments in [("shell", "{}"), ("read_file", "[]"), ("read_file", "not json")]:
            with self.subTest(name=name, arguments=arguments):
                response = copy.deepcopy(self.response)
                response["choices"][0]["message"]["tool_calls"][0]["function"] = {"name": name, "arguments": arguments}
                with patch("learn_agent.loop.call_model", return_value=response), patch("learn_agent.tools.read_file") as reader:
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                        loop.run_agent(self.payload, "offline-placeholder")
                reader.assert_not_called()

    def test_abnormal_finish_does_not_execute_or_continue(self):
        for reason in ("length", "content_filter"):
            with self.subTest(reason=reason):
                response = copy.deepcopy(self.response)
                response["choices"][0]["finish_reason"] = reason
                with patch("learn_agent.loop.call_model", return_value=response) as model, patch("learn_agent.tools.read_file") as reader:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()) as output:
                        loop.run_agent(self.payload, "offline-placeholder")
                model.assert_called_once()
                reader.assert_not_called()
                approval.assert_not_called()
                self.assertIn("任务未完成", output.getvalue())

    def test_duplicate_call_ids_are_rejected_before_execution(self):
        self.request["tool_calls"].append(copy.deepcopy(self.request["tool_calls"][0]))
        with patch("learn_agent.loop.call_model", return_value=self.response), patch("learn_agent.tools.read_file") as reader:
            with patch("builtins.input") as approval:
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                    loop.run_agent(self.payload, "offline-placeholder")
        reader.assert_not_called()
        approval.assert_not_called()

    def test_batched_reads_continue_after_error_and_confirm_each_call(self):
        next_call = copy.deepcopy(self.request["tool_calls"][0])
        next_call["id"] = "call_next"
        next_call["function"]["arguments"] = json.dumps({"path": "next_step.txt"})
        self.request["tool_calls"].append(next_call)
        final = {"choices": [{"message": {"content": "已说明读取结果。"}, "finish_reason": "stop"}]}
        log_file = io.StringIO()
        with patch("learn_agent.loop.call_model", side_effect=[self.response, final]) as model:
            with patch("learn_agent.tools.read_file", side_effect=[FileNotFoundError(), "下一步任务"]) as reader:
                with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                    reason = loop.run_agent(self.payload, "offline-placeholder", log_file)
        self.assertEqual(reason, "stop")
        self.assertEqual(approval.call_count, 2)
        self.assertEqual(reader.call_count, 2)
        results = model.call_args_list[-1].args[0]["messages"][3:]
        self.assertEqual([entry["tool_call_id"] for entry in results], ["call_test", "call_next"])
        self.assertEqual([json.loads(entry["content"])["ok"] for entry in results], [False, True])
        self.assertEqual(json.loads(results[-1]["content"])["content"], "下一步任务")
        entries = [json.loads(line) for line in log_file.getvalue().splitlines()]
        decisions = [entry for entry in entries if entry["event"] == "tool_approval"]
        self.assertTrue(all(entry["source"] == "user" for entry in decisions))

    def test_batched_writes_each_ask_and_refusal_stops_remaining_calls(self):
        calls = []
        for index, content in enumerate(("第一份报告", "第二份报告")):
            calls.append({"id": f"call_write_{index}", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "report.md", "content": content}),
            }})
        self.request["tool_calls"] = [*calls, *self.request["tool_calls"]]
        log_file = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            workspace = Path(directory)
            self.assertEqual(workspace.resolve().parent, Path(tempfile.gettempdir()).resolve())
            with patch.object(permissions, "WORKSPACE", workspace), patch("learn_agent.loop.call_model", return_value=self.response) as model:
                with patch("learn_agent.tools.read_file") as reader, patch("builtins.input", side_effect=["y", "n"]) as approval:
                    with contextlib.redirect_stdout(io.StringIO()):
                        reason = loop.run_agent(self.payload, "offline-placeholder", log_file, auto_read=True)
            self.assertEqual((workspace / "report.md").read_text(encoding="utf-8"), "第一份报告")
        self.assertEqual(reason, "user_denied")
        model.assert_called_once()
        reader.assert_not_called()
        self.assertEqual(approval.call_count, 2)
        self.assertIn("创建 report.md", approval.call_args_list[0].args[0])
        self.assertIn("覆盖现有 report.md", approval.call_args_list[1].args[0])
        entries = [json.loads(line) for line in log_file.getvalue().splitlines()]
        decisions = [entry for entry in entries if entry["event"] == "tool_approval"]
        self.assertEqual([entry["approved"] for entry in decisions], [True, False])
        self.assertEqual([entry["source"] for entry in decisions], ["user", "user"])
        self.assertEqual([entry["tool_call_id"] for entry in entries if entry["event"] == "tool_result"], ["call_write_0"])

    def test_invalid_later_call_rejects_entire_batch_before_execution(self):
        for name, arguments in [("shell", "{}"), ("read_file", '{"path":"../chat.py"}'), ("read_file", "not json")]:
            with self.subTest(name=name, arguments=arguments):
                response = copy.deepcopy(self.response)
                response["choices"][0]["message"]["tool_calls"].append({
                    "id": "call_invalid", "type": "function", "function": {"name": name, "arguments": arguments},
                })
                with patch("learn_agent.loop.call_model", return_value=response), patch("learn_agent.tools.read_file") as reader:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                        loop.run_agent(self.payload, "offline-placeholder", auto_read=True)
                reader.assert_not_called()
                approval.assert_not_called()

    def test_limit_and_abnormal_finish_do_not_execute_batched_tools(self):
        next_call = copy.deepcopy(self.request["tool_calls"][0])
        next_call["id"] = "call_next"
        self.request["tool_calls"].append(next_call)
        for finish_reason, expected in [("tool_calls", "max_model_calls"), ("length", "abnormal_finish")]:
            with self.subTest(finish_reason=finish_reason):
                self.response["choices"][0]["finish_reason"] = finish_reason
                with patch.object(loop, "MAX_MODEL_CALLS", 1), patch("learn_agent.loop.call_model", return_value=self.response) as model:
                    with patch("learn_agent.tools.read_file") as reader, patch("builtins.input") as approval:
                        with contextlib.redirect_stdout(io.StringIO()):
                            reason = loop.run_agent(self.payload, "offline-placeholder", auto_read=True)
                self.assertEqual(reason, expected)
                model.assert_called_once()
                reader.assert_not_called()
                approval.assert_not_called()

    def test_reader_rejects_other_files(self):
        with self.assertRaises(ValueError):
            tools.read_file("../chat.py")

    def test_log_records_order_history_and_tool_results_without_credentials(self):
        final = {"choices": [{"message": {"content": "检查完毕。"}, "finish_reason": "stop"}], "usage": {"total_tokens": 123}}
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds), tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
                self.assertEqual(Path(directory).resolve().parent, Path(tempfile.gettempdir()).resolve())
                log_dir = Path(directory) / "logs"
                with patch.object(tool_call, "LOG_DIR", log_dir), patch.object(sys, "argv", ["tool_call.py"]):
                    with patch("tool_call.get_api_key", return_value="offline-sensitive-test-key"):
                        with patch("learn_agent.loop.call_model", side_effect=[self.response, final]):
                            with patch("learn_agent.tools.read_file", return_value="项目名称：小灯\n实验编号：7391", side_effect=None if succeeds else FileNotFoundError()):
                                with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
                                    tool_call.main()
                paths = list(log_dir.glob("*.jsonl"))
                self.assertEqual(len(paths), 1)
                text = paths[0].read_text(encoding="utf-8")
                entries = [json.loads(line) for line in text.splitlines()]
                self.assertEqual([entry["event"] for entry in entries], [
                    "run_start", "model_request", "model_response", "tool_approval", "tool_result",
                    "model_request", "model_response", "run_end",
                ])
                self.assertTrue(all(entry["time"].endswith("+00:00") for entry in entries))
                self.assertEqual(entries[3]["tool_call_id"], "call_test")
                self.assertTrue(entries[3]["approved"])
                self.assertEqual(entries[3]["source"], "user")
                self.assertFalse(entries[0]["auto_read"])
                self.assertEqual(entries[4]["result"]["ok"], succeeds)
                self.assertEqual(entries[4]["tool_call_id"], "call_test")
                history = entries[5]["payload"]["messages"]
                self.assertEqual(json.loads(history[-1]["content"]), entries[4]["result"])
                self.assertEqual(entries[6]["response"]["usage"], {"total_tokens": 123})
                self.assertEqual(entries[-1]["reason"], "stop")
                self.assertNotIn("offline-sensitive-test-key", text)
                self.assertNotIn("Authorization", text)
                if succeeds:
                    self.assertIn("项目名称：小灯", text)

    def test_log_records_refusal_without_executing_tool(self):
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            self.assertEqual(Path(directory).resolve().parent, Path(tempfile.gettempdir()).resolve())
            log_dir = Path(directory) / "logs"
            with patch.object(tool_call, "LOG_DIR", log_dir), patch.object(sys, "argv", ["tool_call.py"]):
                with patch("tool_call.get_api_key", return_value="offline-placeholder"):
                    with patch("learn_agent.loop.call_model", return_value=self.response) as model, patch("learn_agent.tools.read_file") as reader:
                        with patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
                            tool_call.main()
            entries = [json.loads(line) for line in next(log_dir.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        model.assert_called_once()
        reader.assert_not_called()
        self.assertFalse(entries[-2]["approved"])
        self.assertEqual(entries[-1]["reason"], "user_denied")
        self.assertNotIn("tool_result", [entry["event"] for entry in entries])

    def test_log_records_exception_type_and_cancellation(self):
        for error, reason in [(TimeoutError("private-error-detail"), "error"), (ValueError("invalid request"), "error"), (KeyboardInterrupt(), "cancelled")]:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
                self.assertEqual(Path(directory).resolve().parent, Path(tempfile.gettempdir()).resolve())
                log_dir = Path(directory) / "logs"
                with patch.object(tool_call, "LOG_DIR", log_dir), patch.object(sys, "argv", ["tool_call.py"]):
                    with patch("tool_call.get_api_key", return_value="offline-placeholder"):
                        with patch("learn_agent.loop.call_model", side_effect=error), contextlib.redirect_stdout(io.StringIO()):
                            with self.assertRaises(SystemExit):
                                tool_call.main()
                text = next(log_dir.glob("*.jsonl")).read_text(encoding="utf-8")
                entries = [json.loads(line) for line in text.splitlines()]
                self.assertEqual([entry["event"] for entry in entries], ["run_start", "model_request", "run_end"])
                self.assertEqual(entries[-1]["reason"], reason)
                self.assertEqual(entries[-1]["error_type"], type(error).__name__)
                self.assertNotIn("private-error-detail", text)

    def test_log_records_limit_and_abnormal_finish(self):
        for finish_reason, reason in [("tool_calls", "max_model_calls"), ("length", "abnormal_finish")]:
            with self.subTest(finish_reason=finish_reason), tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
                self.assertEqual(Path(directory).resolve().parent, Path(tempfile.gettempdir()).resolve())
                log_dir = Path(directory) / "logs"
                response = copy.deepcopy(self.response)
                response["choices"][0]["finish_reason"] = finish_reason
                with patch.object(tool_call, "LOG_DIR", log_dir), patch.object(loop, "MAX_MODEL_CALLS", 1):
                    with patch.object(sys, "argv", ["tool_call.py"]), patch("tool_call.get_api_key", return_value="offline-placeholder"):
                        with patch("learn_agent.loop.call_model", return_value=response) as model:
                            with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                                tool_call.main()
                entries = [json.loads(line) for line in next(log_dir.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
                model.assert_called_once()
                approval.assert_not_called()
                self.assertEqual(entries[-1]["reason"], reason)
                self.assertEqual(entries[2]["response"]["choices"][0]["finish_reason"], finish_reason)

    def test_runs_create_separate_logs(self):
        final = {"choices": [{"message": {"content": "你好。"}, "finish_reason": "stop"}]}
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            self.assertEqual(Path(directory).resolve().parent, Path(tempfile.gettempdir()).resolve())
            log_dir = Path(directory) / "logs"
            with patch.object(tool_call, "LOG_DIR", log_dir), patch.object(sys, "argv", ["tool_call.py", "你好"]):
                with patch("tool_call.get_api_key", return_value="offline-placeholder"), patch("learn_agent.loop.call_model", return_value=final):
                    with contextlib.redirect_stdout(io.StringIO()):
                        tool_call.main()
                        tool_call.main()
            paths = list(log_dir.glob("*.jsonl"))
            self.assertEqual(len(paths), 2)
            for path in paths:
                entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(entries[0]["event"], "run_start")
                self.assertEqual(entries[-1]["reason"], "stop")

    def test_preview_does_not_create_log_directory(self):
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            self.assertEqual(Path(directory).resolve().parent, Path(tempfile.gettempdir()).resolve())
            log_dir = Path(directory) / "logs"
            with patch.object(tool_call, "LOG_DIR", log_dir), patch.object(sys, "argv", ["tool_call.py", "--preview"]):
                with patch("tool_call.get_api_key") as key, patch("learn_agent.loop.call_model") as model:
                    with contextlib.redirect_stdout(io.StringIO()):
                        tool_call.main()
            self.assertFalse(log_dir.exists())
        key.assert_not_called()
        model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
