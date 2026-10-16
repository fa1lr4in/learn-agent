import contextlib
import io
import json
import sys
import unittest
from unittest.mock import patch

import chat


class ChatTests(unittest.TestCase):
    def run_chat(self, arguments, replies, inputs):
        def respond(request, timeout):
            response = {
                "choices": [{"message": {"role": "assistant", "content": next(replies)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
            return io.BytesIO(json.dumps(response).encode("utf-8"))

        with patch.object(sys, "argv", ["chat.py", *arguments]):
            with patch("chat.get_api_key", return_value="offline-placeholder") as key:
                with patch("chat.urllib.request.urlopen", side_effect=respond) as transport:
                    with patch("builtins.input", side_effect=inputs) as user_input:
                        with contextlib.redirect_stdout(io.StringIO()):
                            chat.main()
        requests = [json.loads(call.args[0].data) for call in transport.call_args_list]
        return requests, key, user_input

    def test_history_is_sent_on_next_request(self):
        requests, key, _ = self.run_chat(
            ["--chat", "我的项目叫小灯。"],
            iter(["记住了。", "你的项目叫小灯。"]),
            ["我的项目叫什么？", "/exit"],
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual([message["role"] for message in requests[0]["messages"]], ["system", "user"])
        history = requests[1]["messages"]
        self.assertEqual([message["role"] for message in history], ["system", "user", "assistant", "user"])
        self.assertEqual([message["content"] for message in history[1:]], ["我的项目叫小灯。", "记住了。", "我的项目叫什么？"])
        key.assert_called_once()

    def test_clear_keeps_system_and_sends_only_new_question(self):
        requests, key, _ = self.run_chat(
            ["--chat", "我的项目叫小灯。"],
            iter(["记住了。", "你还没有告诉我项目名。"]),
            ["/clear", "我的项目叫什么？", "/exit"],
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            requests[1]["messages"],
            [requests[0]["messages"][0], {"role": "user", "content": "我的项目叫什么？"}],
        )
        key.assert_called_once()

    def test_clear_does_not_send_request(self):
        requests, _, _ = self.run_chat(["--chat", "你好"], iter(["你好。"]), ["/clear", "/exit"])
        self.assertEqual(len(requests), 1)

    def test_single_request_does_not_wait_for_followup(self):
        requests, _, user_input = self.run_chat(["你好"], iter(["你好。"]), [])
        self.assertEqual(len(requests), 1)
        user_input.assert_not_called()

    def test_preview_does_not_read_key_or_start_chat(self):
        requests, key, user_input = self.run_chat(["--chat", "--preview", "你好"], iter([]), [])
        self.assertEqual(requests, [])
        key.assert_not_called()
        user_input.assert_not_called()

    def test_empty_input_does_not_send_another_request(self):
        requests, _, _ = self.run_chat(["--chat", "你好"], iter(["你好。"]), ["  ", "/exit"])
        self.assertEqual(len(requests), 1)

    def test_terminal_interrupt_ends_chat(self):
        for interruption in (EOFError, KeyboardInterrupt):
            with self.subTest(interruption=interruption):
                requests, _, _ = self.run_chat(["--chat", "你好"], iter(["你好。"]), [interruption()])
                self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
