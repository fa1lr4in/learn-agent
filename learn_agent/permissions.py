"""Workspace boundaries and per-call approval policy."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


DEFAULT_WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
WORKSPACE = DEFAULT_WORKSPACE
READ_ONLY_FILES = ("project.txt", "next_step.txt", "test_main.py", "demo_project/test_stats.py")


@contextmanager
def use_workspace(path: str | Path | None = None):
    """Select one existing root for this CLI run, then restore the previous root."""
    global WORKSPACE
    if path is not None and not str(path).strip():
        raise ValueError("工作目录路径不能为空。")
    root = (Path(path).expanduser() if path is not None else WORKSPACE).resolve()
    if not root.is_dir():
        raise ValueError("工作目录不存在或不是目录；请先创建项目目录。")
    previous = WORKSPACE
    WORKSPACE = root
    try:
        yield root
    finally:
        WORKSPACE = previous


def read_only_paths() -> list[str]:
    """Describe fixed lesson files relative to the currently selected root."""
    root = WORKSPACE.resolve()
    protected = [(DEFAULT_WORKSPACE / name).resolve() for name in READ_ONLY_FILES]
    return [path.relative_to(root).as_posix() for path in protected if path.is_relative_to(root)]


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
    if target in (WORKSPACE.resolve() / name for name in read_only_paths()):
        raise ValueError("课程输入资料和固定验收脚本只读，不能通过文件工具修改。")
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


@dataclass(frozen=True)
class PermissionPolicy:
    """Use local launch options and trusted tool metadata, never model text."""
    auto_read: bool = False
    denied_tools: tuple[str, ...] = ()

    def decide(self, name: str, read_only: bool) -> str:
        if name in self.denied_tools:
            return "deny"
        return "allow" if self.auto_read and read_only else "ask"


def approve_tool(action: str, decision: str) -> tuple[bool, str]:
    if decision == "deny":
        print(f"\n[权限策略] 拒绝工具 {action}：本次 --deny-tool 明确禁用；未准备或执行该工具。")
        return False, "policy"
    if decision == "allow":
        print(f"\n[权限策略] 自动允许{action}，结果将发送给 DeepSeek。")
        return True, "auto_read"
    answer = input(f"\n输入 y，允许{action}并把结果发送给 DeepSeek：")
    return answer.strip().lower() == "y", "user"
