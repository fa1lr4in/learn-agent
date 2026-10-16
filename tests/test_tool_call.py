import contextlib
import copy
import io
import json
import sys
import unittest
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
        self.assertEqual(followup["messages"][-1]["content"], tool_call.read_file("project.txt"))
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
        self.assertEqual([history[index]["content"] for index in (3, 5)], ["项目资料", "下一步任务"])
        self.assertEqual(len(self.payload["messages"]), 2)

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
        with patch("tool_call.call_model", return_value=self.response) as model, patch("tool_call.read_file") as reader:
            with patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
                tool_call.run_agent(self.payload, "offline-placeholder")
        model.assert_called_once()
        reader.assert_not_called()

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
            ([], "请先读取 project.txt，再读取其中提到的任务文件，最后汇总项目名称、实验编号和下一步任务。"),
            (["请用一句话介绍自己。"], "请用一句话介绍自己。"),
        ]:
            with self.subTest(arguments=arguments):
                with patch.object(sys, "argv", ["tool_call.py", "--preview", *arguments]):
                    with patch("tool_call.get_api_key") as key, patch("tool_call.call_model") as model:
                        with patch("tool_call.read_file") as reader, contextlib.redirect_stdout(io.StringIO()) as output:
                            tool_call.main()
                payload, _ = json.JSONDecoder().raw_decode(output.getvalue().split("\n", 1)[1])
                self.assertEqual(payload["messages"][-1], {"role": "user", "content": expected})
                key.assert_not_called()
                model.assert_not_called()
                reader.assert_not_called()

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
