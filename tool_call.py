"""Inspect and approve one file-reading tool call."""

import argparse
import json
import os
from pathlib import Path

from chat import call_model, get_api_key


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取练习目录中的 project.txt，获取真实的项目资料。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "enum": ["project.txt"]}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }
]


def read_file(path: str) -> str:
    """This lesson permits only the bundled exercise file."""
    if path != "project.txt":
        raise ValueError("本课只允许读取练习文件 project.txt。")
    return (Path(__file__).parent / "workspace" / "project.txt").read_text(encoding="utf-8")


def run_demo(payload: dict, api_key: str) -> None:
    response = call_model(payload, api_key)
    choice = response["choices"][0]
    message = choice["message"]
    print("\n[第一次模型回复：检查 tool_calls]")
    print(json.dumps(message, ensure_ascii=False, indent=2))
    if choice["finish_reason"] != "tool_calls":
        print(f"\n本次没有进入工具执行，停止原因：{choice['finish_reason']}。")
        return

    calls = message.get("tool_calls", [])
    if len(calls) != 1:
        raise ValueError("本课只演示一次工具调用，请调整提示后重试。")
    tool_call = calls[0]
    function = tool_call["function"]
    if function["name"] != "read_file":
        raise ValueError("未知工具，未执行。")
    arguments = json.loads(function["arguments"])
    if not isinstance(arguments, dict) or set(arguments) != {"path"}:
        raise ValueError("工具参数必须只有一个 path 字段。")
    if arguments["path"] != "project.txt":
        raise ValueError("模型请求的文件不在本课允许范围内，未执行。")

    answer = input("\n输入 y，允许读取练习文件并把内容发送给 DeepSeek：")
    if answer.strip().lower() != "y":
        print("已停止：没有读取文件，也没有发送第二次模型请求。")
        return

    result = read_file(arguments["path"])
    print("\n[Python 工具实际读取的内容]")
    print(result)
    messages = [
        *payload["messages"],
        {"role": "assistant", "content": message.get("content"), "tool_calls": calls},
        {"role": "tool", "tool_call_id": tool_call["id"], "content": result},
    ]
    followup = {**payload, "messages": messages, "tool_choice": "none"}
    print("\n[第二次请求里的消息历史]")
    print(json.dumps(messages, ensure_ascii=False, indent=2))
    response = call_model(followup, api_key)
    choice = response["choices"][0]
    print("\n[模型根据工具结果回答]")
    print(choice["message"].get("content"))
    print(f"停止原因：{choice['finish_reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="执行一次经过确认的工具调用")
    parser.add_argument("--preview", action="store_true", help="查看工具说明，不联网或读取练习文件")
    args = parser.parse_args()
    payload = {
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        "messages": [
            {"role": "system", "content": "需要文件内容时调用工具，收到真实结果后再用中文回答。"},
            {"role": "user", "content": "请读取 project.txt，准确告诉我项目名称和本次实验编号。"},
        ],
        "tools": TOOLS,
        "tool_choice": "auto",
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": 512,
    }
    print("[第一次请求：消息与工具说明]")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.preview:
        print("\n预览结束，没有联网或读取练习文件。")
        return
    try:
        run_demo(payload, get_api_key())
    except (ValueError, OSError, EOFError) as error:
        raise SystemExit(f"本次实验停止：{error}") from None
    except KeyboardInterrupt:
        raise SystemExit("\n已取消本次实验。") from None


if __name__ == "__main__":
    main()
