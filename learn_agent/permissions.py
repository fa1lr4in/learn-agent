"""Workspace boundaries and per-call approval policy."""

from pathlib import Path


WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
READ_ONLY_FILES = ("project.txt", "next_step.txt", "test_main.py", "demo_project/test_stats.py")


def resolve_workspace_path(path: str) -> Path:
    """Resolve a visible relative path while keeping it inside the workspace."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path 必须是非空字符串。")
    relative = Path(path)
    if relative.anchor or ".." in relative.parts or ":" in path:
        raise ValueError("只允许使用 workspace 内的相对路径，不能包含 .. 或盘符。")
    root = WORKSPACE.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("路径解析后超出 workspace，未执行。")
    if any(part.startswith(".") for part in (*relative.parts, *target.relative_to(root).parts)):
        raise ValueError("不允许访问点号开头的文件或目录。")
    return target


def resolve_write_path(path: str) -> Path:
    """Check workspace and read-only boundaries before approving a write."""
    target = resolve_workspace_path(path)
    if target in (WORKSPACE.resolve() / name for name in READ_ONLY_FILES):
        raise ValueError("输入资料和固定验收脚本只读，不能通过 write_file 修改。")
    if (WORKSPACE / path).is_symlink():
        raise ValueError("拒绝写入符号链接，请使用普通文件。")
    return target


def resolve_python_path(path: str) -> Path:
    """Check the workspace boundary and Python suffix before approval."""
    target = resolve_workspace_path(path)
    if target.suffix.lower() != ".py":
        raise ValueError("只允许运行 workspace 内的 .py 文件。")
    if (WORKSPACE / path).is_symlink():
        raise ValueError("拒绝运行符号链接，请使用普通脚本文件。")
    return target


def approve_tool(action: str, read_only: bool, auto_read: bool) -> tuple[bool, str]:
    if auto_read and read_only:
        print(f"\n[权限策略] 自动允许{action}，结果将发送给 DeepSeek。")
        return True, "auto_read"
    answer = input(f"\n输入 y，允许{action}并把结果发送给 DeepSeek：")
    return answer.strip().lower() == "y", "user"
