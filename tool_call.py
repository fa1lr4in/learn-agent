"""Run a bounded agent loop with approved file tool calls."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from chat import call_model, get_api_key


ALLOWED_FILES = ("project.txt", "next_step.txt")
PYTHON_FILE = "main.py"
WRITABLE_FILES = ("report.md", PYTHON_FILE)
WORKSPACE = Path(__file__).parent / "workspace"
LOG_DIR = Path(__file__).parent / "logs"
MAX_MODEL_CALLS = 5
RUN_TIMEOUT = 10

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
            "description": "写入练习目录的 report.md 报告或 main.py 脚本，已有文件会被覆盖。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "enum": list(WRITABLE_FILES)},
                    "content": {"type": "string", "description": "完整的文件内容"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "运行练习目录的 main.py，返回退出码、标准输出和标准错误，最长运行 10 秒。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "enum": [PYTHON_FILE]}},
                "required": ["path"],
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
    """Write a nonempty report or script without following a symbolic link."""
    if path not in WRITABLE_FILES:
        raise ValueError("只允许写入练习文件 report.md 和 main.py。")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("写入内容必须是非空字符串。")
    target = WORKSPACE / path
    if target.is_symlink():
        raise ValueError("拒绝写入符号链接，请使用普通文件。")
    return target.write_text(content, encoding="utf-8")


def run_python(path: str) -> subprocess.CompletedProcess[str]:
    """Run the exercise script with the current interpreter and a timeout."""
    if path != PYTHON_FILE:
        raise ValueError("只允许运行练习脚本 main.py。")
    target = WORKSPACE / path
    if target.is_symlink():
        raise ValueError("拒绝运行符号链接，请使用普通的 main.py 文件。")
    if not target.is_file():
        raise FileNotFoundError("main.py 不存在或不是普通文件。")
    child_env = os.environ.copy()
    child_env.pop("DEEPSEEK_API_KEY", None)
    return subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(target.resolve())],
        cwd=WORKSPACE,
        env=child_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=RUN_TIMEOUT,
    )


def record_event(log_file: TextIO | None, event: str, **details) -> None:
    """Append and flush one JSON event without receiving API credentials."""
    if log_file is None:
        return
    entry = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **details}
    log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log_file.flush()


def run_agent(payload: dict, api_key: str, log_file: TextIO | None = None, auto_read: bool = False) -> str:
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
        if not calls:
            raise ValueError("模型没有提供工具请求，未执行。")
        prepared_calls = []
        call_ids = set()
        for tool_call in calls:
            call_id = tool_call.get("id")
            if not isinstance(call_id, str) or not call_id.strip() or call_id in call_ids:
                raise ValueError("工具调用 ID 必须是非空字符串，且同一回复中不能重复。")
            call_ids.add(call_id)
            function = tool_call["function"]
            name = function["name"]
            if name not in ("read_file", "list_files", "write_file", "run_python"):
                raise ValueError("未知工具，未执行。")
            arguments = json.loads(function["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象。")
            if name == "read_file":
                if set(arguments) != {"path"}:
                    raise ValueError("read_file 参数必须只有一个 path 字段。")
                if arguments["path"] not in ALLOWED_FILES:
                    raise ValueError("模型请求的文件不在允许范围内，未执行。")
            elif name == "list_files":
                if arguments:
                    raise ValueError("list_files 不接受参数，请传入空对象。")
            elif name == "write_file":
                if set(arguments) != {"path", "content"} or arguments["path"] not in WRITABLE_FILES:
                    raise ValueError("write_file 必须提供 path 和 content，且只能写入 report.md 和 main.py。")
                if not isinstance(arguments["content"], str) or not arguments["content"].strip():
                    raise ValueError("写入内容必须是非空字符串。")
            else:
                if set(arguments) != {"path"} or arguments["path"] != PYTHON_FILE:
                    raise ValueError("run_python 参数必须只有 path，且只能运行 main.py。")
            prepared_calls.append((tool_call, name, arguments))

        messages = [
            *request["messages"],
            {"role": "assistant", "content": message.get("content"), "tool_calls": calls},
        ]
        for tool_call, name, arguments in prepared_calls:
            if name == "read_file":
                action = f"读取 {arguments['path']}"
            elif name == "list_files":
                action = "列出可用的练习文件"
            elif name == "write_file":
                path = arguments["path"]
                print(f"\n[即将写入 {path} 的完整内容]")
                print(arguments["content"])
                action = f"覆盖现有 {path}" if (WORKSPACE / path).exists() else f"创建 {path}"
            else:
                action = f"运行 {PYTHON_FILE}（最长 {RUN_TIMEOUT} 秒）"

            if auto_read and name in ("read_file", "list_files"):
                print(f"\n[权限策略] 自动允许{action}，结果将发送给 DeepSeek。")
                approved, source = True, "auto_read"
            else:
                answer = input(f"\n输入 y，允许{action}并把结果发送给 DeepSeek：")
                approved, source = answer.strip().lower() == "y", "user"
            record_event(log_file, "tool_approval", step=step, tool=name, tool_call_id=tool_call["id"], approved=approved, source=source)
            if not approved:
                print("已停止：未执行被拒绝的工具及本轮后续工具，也不会继续请求模型；此前已完成的操作保留。")
                return "user_denied"

            try:
                if name == "read_file":
                    path = arguments["path"]
                    result = {"ok": True, "tool": name, "path": path, "content": read_file(path)}
                elif name == "list_files":
                    result = {"ok": True, "tool": name, "files": list_files()}
                elif name == "write_file":
                    count = write_file(arguments["path"], arguments["content"])
                    result = {"ok": True, "tool": name, "path": arguments["path"], "characters_written": count}
                else:
                    completed = run_python(arguments["path"])
                    result = {
                        "ok": completed.returncode == 0,
                        "tool": name,
                        "path": arguments["path"],
                        "exit_code": completed.returncode,
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                    }
            except subprocess.TimeoutExpired:
                result = {
                    "ok": False,
                    "tool": name,
                    "path": arguments["path"],
                    "error": {"type": "TimeoutExpired", "message": f"运行超过 {RUN_TIMEOUT} 秒，Python 子进程已终止。"},
                }
            except (OSError, UnicodeError) as error:
                result = {
                    "ok": False,
                    "tool": name,
                    "error": {
                        "type": type(error).__name__,
                        "message": "工具执行失败，请检查文件是否存在、访问权限及 UTF-8 编码。",
                    },
                }
                if name in ("read_file", "write_file", "run_python"):
                    result["path"] = arguments["path"]
            print("\n[工具执行结果]")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            record_event(log_file, "tool_result", step=step, tool=name, tool_call_id=tool_call["id"], result=result)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": json.dumps(result, ensure_ascii=False)})
        request = {**request, "messages": messages}


def main() -> None:
    parser = argparse.ArgumentParser(description="运行带权限检查的 Agent 工具循环")
    parser.add_argument("prompt", nargs="?", default="请先列出可用的练习文件，再读取项目资料和其中提到的任务文件，汇总项目名称、实验编号和下一步任务，用 write_file 将 Markdown 报告保存到 report.md，最后说明保存结果。")
    parser.add_argument("--preview", action="store_true", help="查看工具说明，不联网或读写练习文件")
    parser.add_argument("--auto-read", action="store_true", help="自动允许列出及读取两份练习文件并发送结果；写入仍需确认")
    args = parser.parse_args()
    payload = {
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        "messages": [
            {"role": "system", "content": "需要文件内容时调用工具；不确定哪些文件可用时使用 list_files，需要正文时使用 read_file。同一轮可以请求多个互不依赖的工具，程序按顺序执行；依赖工具结果的后续操作，请收到结果后再提出。使用 write_file 保存完整文件，只能写入 report.md 报告或 main.py 脚本；只有写入工具返回 ok 为 true 后才能声称已经保存。需要验证代码时调用 run_python 运行 main.py，根据实际 exit_code、stdout 和 stderr 判断结果，不能凭推测声称运行成功。工具返回 JSON，ok 为 false 表示执行失败，请根据错误调整操作，不要把错误当作文件内容或反复重试同一失败操作。收到足够的真实结果后用中文回答；无法完成的部分如实说明。"},
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
            record_event(log_file, "run_start", max_model_calls=MAX_MODEL_CALLS, auto_read=args.auto_read)
            try:
                reason = run_agent(payload, api_key, log_file, auto_read=args.auto_read)
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
