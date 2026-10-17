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
        with patch("tool_call.call_model", side_effect=[self.response, final]) as model:
            with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
                tool_call.run_agent(self.payload, "offline-placeholder")
        self.assertEqual(model.call_count, 2)
        followup = model.call_args_list[1].args[0]
        self.assertEqual([message["role"] for message in followup["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(followup["messages"][-1]["tool_call_id"], "call_test")
        result = json.loads(followup["messages"][-1]["content"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "project.txt")
        self.assertEqual(result["content"], tool_call.read_file("project.txt"))
        self.assertEqual(followup["tool_choice"], "auto")
        self.assertEqual(len(self.payload["messages"]), 2)

    def test_multiple_rounds_keep_results_and_allow_more_tools(self):
        second = copy.deepcopy(self.response)
        second_call = second["choices"][0]["message"]["tool_calls"][0]
        second_call["id"] = "call_next"
        second_call["function"]["arguments"] = json.dumps({"path": "next_step.txt"})
        final = {"choices": [{"message": {"content": "汇总完成。"}, "finish_reason": "stop"}]}
        with patch("tool_call.call_model", side_effect=[self.response, second, final]) as model:
            with patch("tool_call.read_file", side_effect=["项目资料", "下一步任务"]) as reader:
                with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                    tool_call.run_agent(self.payload, "offline-placeholder")
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

    def test_list_files_only_returns_allowed_existing_files(self):
        with tempfile.TemporaryDirectory(prefix="learn-agent-test-") as directory:
            workspace = Path(directory)
            self.assertEqual(workspace.resolve().parent, Path(tempfile.gettempdir()).resolve())
            (workspace / "project.txt").write_text("项目资料", encoding="utf-8")
            (workspace / "private.txt").write_text("不允许读取", encoding="utf-8")
            (workspace / "next_step.txt").mkdir()
            with patch.object(tool_call, "WORKSPACE", workspace):
                self.assertEqual(tool_call.list_files(), ["project.txt"])

    def test_list_then_read_dispatches_to_correct_functions(self):
        listing = copy.deepcopy(self.response)
        listing_call = listing["choices"][0]["message"]["tool_calls"][0]
        listing_call["id"] = "call_list"
        listing_call["function"] = {"name": "list_files", "arguments": "{}"}
        final = {"choices": [{"message": {"content": "项目叫小灯。"}, "finish_reason": "stop"}]}
        with patch("tool_call.call_model", side_effect=[listing, self.response, final]) as model:
            with patch("tool_call.list_files", return_value=["project.txt"]) as lister:
                with patch("tool_call.read_file", return_value="项目名称：小灯") as reader:
                    with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                        tool_call.run_agent(self.payload, "offline-placeholder")
        lister.assert_called_once_with()
        reader.assert_called_once_with("project.txt")
        self.assertEqual(approval.call_count, 2)
        self.assertEqual(model.call_count, 3)
        history = model.call_args_list[-1].args[0]["messages"]
        self.assertEqual([history[index]["tool_call_id"] for index in (3, 5)], ["call_list", "call_test"])
        listed, read = [json.loads(history[index]["content"]) for index in (3, 5)]
        self.assertEqual(listed, {"ok": True, "tool": "list_files", "files": ["project.txt"]})
        self.assertEqual(read["tool"], "read_file")
        self.assertEqual(read["content"], "项目名称：小灯")

    def test_list_files_rejects_arguments_before_confirmation(self):
        self.request["tool_calls"][0]["function"] = {"name": "list_files", "arguments": '{"path":".."}'}
        with patch("tool_call.call_model", return_value=self.response), patch("tool_call.list_files") as lister:
            with patch("builtins.input") as approval:
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                    tool_call.run_agent(self.payload, "offline-placeholder")
        lister.assert_not_called()
        approval.assert_not_called()

    def test_list_failure_is_returned_as_tool_result(self):
        self.request["tool_calls"][0]["function"] = {"name": "list_files", "arguments": "{}"}
        final = {"choices": [{"message": {"content": "无法列出文件。"}, "finish_reason": "stop"}]}
        with patch("tool_call.call_model", side_effect=[self.response, final]) as model:
            with patch("tool_call.list_files", side_effect=PermissionError()):
                with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
                    tool_call.run_agent(self.payload, "offline-placeholder")
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
                with patch("tool_call.call_model", side_effect=[self.response, final]) as model:
                    with patch("tool_call.read_file", side_effect=error):
                        with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
                            tool_call.run_agent(self.payload, "offline-placeholder")
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
        with patch("tool_call.call_model", side_effect=[first, self.response, final]) as model:
            with patch("tool_call.read_file", side_effect=[FileNotFoundError(), "项目名称：小灯"]) as reader:
                with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()):
                    tool_call.run_agent(self.payload, "offline-placeholder")
        self.assertEqual(model.call_count, 3)
        self.assertEqual(approval.call_count, 2)
        self.assertEqual([call.args[0] for call in reader.call_args_list], ["next_step.txt", "project.txt"])
        history = model.call_args_list[-1].args[0]["messages"]
        failed, succeeded = [json.loads(history[index]["content"]) for index in (3, 5)]
        self.assertFalse(failed["ok"])
        self.assertTrue(succeeded["ok"])
        self.assertEqual(succeeded["content"], "项目名称：小灯")

    def test_repeated_read_errors_still_respect_request_limit(self):
        with patch("tool_call.call_model", return_value=self.response) as model:
            with patch("tool_call.read_file", side_effect=FileNotFoundError()) as reader:
                with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()) as output:
                    tool_call.run_agent(self.payload, "offline-placeholder")
        self.assertEqual(model.call_count, tool_call.MAX_MODEL_CALLS)
        self.assertEqual(reader.call_count, tool_call.MAX_MODEL_CALLS - 1)
        self.assertIn("任务未完成", output.getvalue())

    def test_limit_stops_before_executing_another_tool(self):
        with patch.object(tool_call, "MAX_MODEL_CALLS", 3):
            with patch("tool_call.call_model", return_value=self.response) as model:
                with patch("tool_call.read_file", return_value="项目资料") as reader:
                    with patch("builtins.input", return_value="y") as approval, contextlib.redirect_stdout(io.StringIO()) as output:
                        tool_call.run_agent(self.payload, "offline-placeholder")
        self.assertEqual(model.call_count, 3)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(approval.call_count, 2)
        self.assertIn("已达到最多 3 次模型请求，任务未完成", output.getvalue())

    def test_refusal_does_not_read_or_call_model_again(self):
        for name, arguments in [("read_file", '{"path":"project.txt"}'), ("list_files", "{}")]:
            with self.subTest(name=name):
                response = copy.deepcopy(self.response)
                response["choices"][0]["message"]["tool_calls"][0]["function"] = {"name": name, "arguments": arguments}
                with patch("tool_call.call_model", return_value=response) as model:
                    with patch("tool_call.read_file") as reader, patch("tool_call.list_files") as lister:
                        with patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
                            tool_call.run_agent(self.payload, "offline-placeholder")
                model.assert_called_once()
                reader.assert_not_called()
                lister.assert_not_called()

    def test_text_reply_does_not_execute_tool_or_ask_for_confirmation(self):
        response = {"choices": [{"message": {"role": "assistant", "content": "你好。"}, "finish_reason": "stop"}]}
        with patch("tool_call.call_model", return_value=response) as model, patch("tool_call.read_file") as reader:
            with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                tool_call.run_agent(self.payload, "offline-placeholder")
        model.assert_called_once()
        reader.assert_not_called()
        approval.assert_not_called()

    def test_preview_shows_question_without_key_network_or_file_access(self):
        for arguments, expected in [
            ([], "请先列出可用的练习文件，再读取项目资料和其中提到的任务文件，最后汇总项目名称、实验编号和下一步任务。"),
            (["请用一句话介绍自己。"], "请用一句话介绍自己。"),
        ]:
            with self.subTest(arguments=arguments):
                with patch.object(sys, "argv", ["tool_call.py", "--preview", *arguments]):
                    with patch("tool_call.get_api_key") as key, patch("tool_call.call_model") as model:
                        with patch("tool_call.read_file") as reader, patch("tool_call.list_files") as lister, contextlib.redirect_stdout(io.StringIO()) as output:
                            tool_call.main()
                payload, _ = json.JSONDecoder().raw_decode(output.getvalue().split("\n", 1)[1])
                self.assertEqual(payload["messages"][-1], {"role": "user", "content": expected})
                key.assert_not_called()
                model.assert_not_called()
                reader.assert_not_called()
                lister.assert_not_called()

    def test_unapproved_path_is_rejected_before_confirmation(self):
        self.request["tool_calls"][0]["function"]["arguments"] = json.dumps({"path": "../chat.py"})
        with patch("tool_call.call_model", return_value=self.response), patch("builtins.input") as approval:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                tool_call.run_agent(self.payload, "offline-placeholder")
        approval.assert_not_called()

    def test_unknown_tool_and_invalid_arguments_are_rejected(self):
        for name, arguments in [("shell", "{}"), ("read_file", "[]"), ("read_file", "not json")]:
            with self.subTest(name=name, arguments=arguments):
                response = copy.deepcopy(self.response)
                response["choices"][0]["message"]["tool_calls"][0]["function"] = {"name": name, "arguments": arguments}
                with patch("tool_call.call_model", return_value=response), patch("tool_call.read_file") as reader:
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                        tool_call.run_agent(self.payload, "offline-placeholder")
                reader.assert_not_called()

    def test_abnormal_finish_does_not_execute_or_continue(self):
        for reason in ("length", "content_filter"):
            with self.subTest(reason=reason):
                response = copy.deepcopy(self.response)
                response["choices"][0]["finish_reason"] = reason
                with patch("tool_call.call_model", return_value=response) as model, patch("tool_call.read_file") as reader:
                    with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()) as output:
                        tool_call.run_agent(self.payload, "offline-placeholder")
                model.assert_called_once()
                reader.assert_not_called()
                approval.assert_not_called()
                self.assertIn("任务未完成", output.getvalue())

    def test_multiple_calls_in_one_reply_are_rejected_before_execution(self):
        self.request["tool_calls"].append(copy.deepcopy(self.request["tool_calls"][0]))
        with patch("tool_call.call_model", return_value=self.response), patch("tool_call.read_file") as reader:
            with patch("builtins.input") as approval:
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                    tool_call.run_agent(self.payload, "offline-placeholder")
        reader.assert_not_called()
        approval.assert_not_called()

    def test_reader_rejects_other_files(self):
        with self.assertRaises(ValueError):
            tool_call.read_file("../chat.py")


if __name__ == "__main__":
    unittest.main()
