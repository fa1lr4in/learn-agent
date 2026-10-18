"""Run foreground argv commands with bounded output and process cleanup."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .permissions import resolve_workspace_path


COMMAND_TIMEOUT = 30
MAX_OUTPUT_BYTES = 16 * 1024
POLL_INTERVAL = 0.05


def command_args(command: list[str], cwd: Path) -> list[str]:
    program = command[0]
    if program.lower() in ("python", "python3", "python.exe"):
        program = sys.executable
    elif not Path(program).is_absolute() and ("/" in program or "\\" in program):
        program = str((cwd / program).resolve())
    else:
        program = shutil.which(program) or program
    if Path(program).suffix.lower() in (".bat", ".cmd"):
        raise ValueError("run_command 不直接运行 .bat 或 .cmd；请提供可执行程序及其参数。")
    return [program, *command[1:]]


def validate_command(command: list[str], path: str = ".", timeout: int = COMMAND_TIMEOUT) -> None:
    if not isinstance(command, list) or not command or any(not isinstance(part, str) or "\x00" in part for part in command):
        raise ValueError("command 必须是非空字符串数组，不能包含 NUL 字符。")
    if not command[0].strip():
        raise ValueError("command 的第一个元素必须是程序名。")
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError("timeout 必须是 1 至 120 的整数秒。")
    cwd = resolve_workspace_path(path)
    if not cwd.is_dir():
        raise ValueError("命令工作目录不存在或不是目录。")
    command_args(command, cwd)


def stop_process_tree(process: subprocess.Popen) -> bool:
    """Stop the running foreground process tree, then reap the direct child."""
    cleaned = True
    try:
        if os.name == "nt":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
            stopped = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={key: value for key, value in os.environ.items() if key.upper() != "DEEPSEEK_API_KEY"},
                timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            cleaned = stopped.returncode == 0
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        cleaned = False
    finally:
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            cleaned = False
    return cleaned


def run_command(command: list[str], path: str = ".", timeout: int = COMMAND_TIMEOUT) -> dict:
    validate_command(command, path, timeout)
    cwd = resolve_workspace_path(path)
    argv = command_args(command, cwd)
    result = {"ok": False, "command": argv, "path": path, "cwd": str(cwd), "exit_code": None,
              "stdout": "", "stderr": "", "timed_out": False,
              "stdout_truncated": False, "stderr_truncated": False}
    output_error = {"type": "OutputLimitExceeded", "message": f"命令输出超过每个流 {MAX_OUTPUT_BYTES} 字节的上限，结果不完整；请缩小任务或减少输出。"}
    child_env = os.environ.copy()
    child_env.pop("DEEPSEEK_API_KEY", None)
    child_env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                argv, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                shell=False, start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as error:
            result["error"] = {"type": type(error).__name__, "message": "无法启动命令，请检查可执行程序、工作目录和访问权限。"}
            return result
        deadline = time.monotonic() + timeout
        try:
            while True:
                if any(os.fstat(stream.fileno()).st_size > MAX_OUTPUT_BYTES for stream in (stdout, stderr)):
                    result["error"] = output_error
                    break
                if process.poll() is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result["timed_out"] = True
                    result["error"] = {"type": "TimeoutExpired", "message": f"命令运行超过 {timeout} 秒，已请求终止进程树。"}
                    break
                try:
                    process.wait(timeout=min(POLL_INTERVAL, remaining))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                result["cleanup_succeeded"] = stop_process_tree(process)
        for name, stream in (("stdout", stdout), ("stderr", stderr)):
            stream.seek(0)
            data = stream.read(MAX_OUTPUT_BYTES + 1)
            result[name + "_truncated"] = len(data) > MAX_OUTPUT_BYTES
            result[name] = data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        if result["stdout_truncated"] or result["stderr_truncated"]:
            result.setdefault("error", output_error)
        result["exit_code"] = process.returncode
        result["ok"] = process.returncode == 0 and "error" not in result
    return result
