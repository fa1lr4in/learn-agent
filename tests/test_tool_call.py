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
                tool_call.run_demo(self.payload, "offline-placeholder")
        self.assertEqual(model.call_count, 2)
        followup = model.call_args_list[1].args[0]
        self.assertEqual([message["role"] for message in followup["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(followup["messages"][-1]["tool_call_id"], "call_test")
        self.assertEqual(followup["messages"][-1]["content"], tool_call.read_file("project.txt"))
        self.assertEqual(followup["tool_choice"], "none")
        self.assertEqual(len(self.payload["messages"]), 2)

    def test_refusal_does_not_read_or_call_model_again(self):
        with patch("tool_call.call_model", return_value=self.response) as model, patch("tool_call.read_file") as reader:
            with patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
                tool_call.run_demo(self.payload, "offline-placeholder")
        model.assert_called_once()
        reader.assert_not_called()

    def test_text_reply_does_not_execute_tool_or_ask_for_confirmation(self):
        response = {"choices": [{"message": {"role": "assistant", "content": "你好。"}, "finish_reason": "stop"}]}
        with patch("tool_call.call_model", return_value=response) as model, patch("tool_call.read_file") as reader:
            with patch("builtins.input") as approval, contextlib.redirect_stdout(io.StringIO()):
                tool_call.run_demo(self.payload, "offline-placeholder")
        model.assert_called_once()
        reader.assert_not_called()
        approval.assert_not_called()

    def test_preview_shows_question_without_key_network_or_file_access(self):
        for arguments, expected in [
            ([], "请读取 project.txt，准确告诉我项目名称和本次实验编号。"),
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
                tool_call.run_demo(self.payload, "offline-placeholder")
        approval.assert_not_called()

    def test_unknown_tool_and_invalid_arguments_are_rejected(self):
        for name, arguments in [("shell", "{}"), ("read_file", "[]"), ("read_file", "not json")]:
            with self.subTest(name=name, arguments=arguments):
                response = copy.deepcopy(self.response)
                response["choices"][0]["message"]["tool_calls"][0]["function"] = {"name": name, "arguments": arguments}
                with patch("tool_call.call_model", return_value=response), patch("tool_call.read_file") as reader:
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                        tool_call.run_demo(self.payload, "offline-placeholder")
                reader.assert_not_called()

    def test_reader_rejects_other_files(self):
        with self.assertRaises(ValueError):
            tool_call.read_file("../chat.py")


if __name__ == "__main__":
    unittest.main()
