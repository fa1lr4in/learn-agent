"""Run a bounded agent loop with approved file tool calls."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from chat import call_model, get_api_key


ALLOWED_FILES = ("project.txt", "next_step.txt")
OUTPUT_FILE = "report.md"
WORKSPACE = Path(__file__).parent / "workspace"
LOG_DIR = Path(__file__).parent / "logs"
MAX_MODEL_CALLS = 5

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取练习目录中的项目资料或任务文件。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "enum": list(ALLOWED_FILES)}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出练习目录中允许读取且实际存在的文件名，不读取正文。",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "将整理好的报告写入练习目录的 report.md，已有文件会被覆盖。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "enum": [OUTPUT_FILE]},
                    "content": {"type": "string", "description": "完整的 Markdown 报告内容"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
]


def read_file(path: str) -> str:
    """Read only the bundled exercise files."""
    if path not in ALLOWED_FILES:
        raise ValueError("只允许读取练习文件 project.txt 和 next_step.txt。")
    return (WORKSPACE / path).read_text(encoding="utf-8")


def list_files() -> list[str]:
    """List existing files from the same allowlist used by read_file."""
    return [name for name in ALLOWED_FILES if (WORKSPACE / name).is_file()]


def write_file(path: str, content: str) -> int:
    """Write a nonempty report without following an existing symbolic link."""
    if path != OUTPUT_FILE:
        raise ValueError("只允许写入练习文件 report.md。")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("写入内容必须是非空字符串。")
    target = WORKSPACE / path
    if target.is_symlink():
        raise ValueError("拒绝写入符号链接，请使用普通的 report.md 文件。")
    return target.write_text(content, encoding="utf-8")


def record_event(log_file: TextIO | None, event: str, **details) -> None:
    """Append and flush one JSON event without receiving API credentials."""
    if log_file is None:
        return
    entry = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **details}
    log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log_file.flush()


def run_agent(payload: dict, api_key: str, log_file: TextIO | None = None) -> str:
    request = {**payload, "tool_choice": "auto"}
    for step in range(1, MAX_MODEL_CALLS + 1):
        if step > 1:
            print(f"\n[第 {step} 次请求里的消息历史]")
            print(json.dumps(request["messages"], ensure_ascii=False, indent=2))
        record_event(log_file, "model_request", step=step, payload=request)
        response = call_model(request, api_key)
        record_event(log_file, "model_response", step=step, response=response)
        choice = response["choices"][0]
        message = choice["message"]
        print(f"\n[第 {step} 次模型回复]")
        print(json.dumps(message, ensure_ascii=False, indent=2))
        finish_reason = choice["finish_reason"]
        print(f"停止原因：{finish_reason}")
        if finish_reason == "stop":
            print("\n[助手最终回答]")
            print(message.get("content") or "（本次没有返回文本回答）")
            return "stop"
        if finish_reason != "tool_calls":
            print("本次任务未完成：模型输出被截断或请求异常结束，未执行本轮工具。")
            return "abnormal_finish"
        if step == MAX_MODEL_CALLS:
            print(f"已达到最多 {MAX_MODEL_CALLS} 次模型请求，任务未完成；未执行本轮工具。")
            return "max_model_calls"

        calls = message.get("tool_calls") or []
        if len(calls) != 1:
            raise ValueError("当前每轮只处理一个工具请求，请让模型分轮调用。")
        tool_call = calls[0]
        function = tool_call["function"]
        name = function["name"]
        if name not in ("read_file", "list_files", "write_file"):
            raise ValueError("未知工具，未执行。")
        arguments = json.loads(function["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须是 JSON 对象。")
        if name == "read_file":
            if set(arguments) != {"path"}:
                raise ValueError("read_file 参数必须只有一个 path 字段。")
            if arguments["path"] not in ALLOWED_FILES:
                raise ValueError("模型请求的文件不在允许范围内，未执行。")
            action = f"读取 {arguments['path']}"
        elif name == "list_files":
            if arguments:
                raise ValueError("list_files 不接受参数，请传入空对象。")
            action = "列出可用的练习文件"
        else:
            if set(arguments) != {"path", "content"} or arguments["path"] != OUTPUT_FILE:
                raise ValueError("write_file 必须提供 path 和 content，且只能写入 report.md。")
            if not isinstance(arguments["content"], str) or not arguments["content"].strip():
                raise ValueError("写入内容必须是非空字符串。")
            print("\n[即将写入 report.md 的完整内容]")
            print(arguments["content"])
            action = "覆盖现有 report.md" if (WORKSPACE / OUTPUT_FILE).exists() else "创建 report.md"

        answer = input(f"\n输入 y，允许{action}并把结果发送给 DeepSeek：")
        approved = answer.strip().lower() == "y"
        record_event(log_file, "tool_approval", step=step, tool=name, tool_call_id=tool_call["id"], approved=approved)
        if not approved:
            print("已停止：本轮没有执行工具，也不会继续请求模型。")
            return "user_denied"

        try:
            if name == "read_file":
                path = arguments["path"]
                result = {"ok": True, "tool": name, "path": path, "content": read_file(path)}
            elif name == "list_files":
                result = {"ok": True, "tool": name, "files": list_files()}
            else:
                count = write_file(arguments["path"], arguments["content"])
                result = {"ok": True, "tool": name, "path": arguments["path"], "characters_written": count}
        except (OSError, UnicodeError) as error:
            result = {
                "ok": False,
                "tool": name,
                "error": {
                    "type": type(error).__name__,
                    "message": "文件工具执行失败，请检查文件是否存在、访问权限及 UTF-8 编码。",
                },
            }
            if name in ("read_file", "write_file"):
                result["path"] = arguments["path"]
        print("\n[工具执行结果]")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        record_event(log_file, "tool_result", step=step, tool=name, tool_call_id=tool_call["id"], result=result)
        messages = [
            *request["messages"],
            {"role": "assistant", "content": message.get("content"), "tool_calls": calls},
            {"role": "tool", "tool_call_id": tool_call["id"], "content": json.dumps(result, ensure_ascii=False)},
        ]
        request = {**request, "messages": messages}


def main() -> None:
    parser = argparse.ArgumentParser(description="运行经过逐次确认的 Agent 工具循环")
    parser.add_argument("prompt", nargs="?", default="请先列出可用的练习文件，再读取项目资料和其中提到的任务文件，汇总项目名称、实验编号和下一步任务，用 write_file 将 Markdown 报告保存到 report.md，最后说明保存结果。")
    parser.add_argument("--preview", action="store_true", help="查看工具说明，不联网或读写练习文件")
    args = parser.parse_args()
    payload = {
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        "messages": [
            {"role": "system", "content": "需要文件内容时调用工具，每轮最多调用一次；不确定哪些文件可用时使用 list_files，需要正文时使用 read_file。如需多个文件，分轮读取。需要保存报告时使用 write_file，只能写入 report.md，content 必须包含完整报告；只有写入工具返回 ok 为 true 后才能声称已经保存。工具返回 JSON，ok 为 false 表示执行失败，请根据错误调整操作，不要把错误当作文件内容或反复重试同一失败操作。收到足够的真实结果后用中文回答；无法完成的部分如实说明。"},
            {"role": "user", "content": args.prompt},
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
        print("\n预览结束，没有联网或读写练习文件。")
        return
    try:
        api_key = get_api_key()
        LOG_DIR.mkdir(exist_ok=True)
        log_path = LOG_DIR / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.jsonl"
        with log_path.open("x", encoding="utf-8") as log_file:
            print(f"\n[本次运行日志] {log_path}")
            record_event(log_file, "run_start", max_model_calls=MAX_MODEL_CALLS)
            try:
                reason = run_agent(payload, api_key, log_file)
            except (ValueError, OSError, EOFError, KeyboardInterrupt) as error:
                reason = "cancelled" if isinstance(error, KeyboardInterrupt) else "error"
                record_event(log_file, "run_end", reason=reason, error_type=type(error).__name__)
                raise
            record_event(log_file, "run_end", reason=reason)
    except (ValueError, OSError, EOFError) as error:
        raise SystemExit(f"本次实验停止：{error}") from None
    except KeyboardInterrupt:
        raise SystemExit("\n已取消本次实验。") from None


if __name__ == "__main__":
    main()
