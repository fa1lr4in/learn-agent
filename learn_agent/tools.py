"""Built-in tools and the registry used by the agent loop."""

import difflib
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .permissions import resolve_python_path, resolve_workspace_path, resolve_write_path


RUN_TIMEOUT = 10
SEARCH_IGNORED_DIRS = {"node_modules", "__pycache__", "venv", "build", "dist"}
MAX_SEARCH_FILE_BYTES = 256 * 1024


def read_file(path: str) -> str:
    """Read a visible UTF-8 file inside the workspace."""
    target = resolve_workspace_path(path)
    if not target.is_file():
        raise FileNotFoundError("目标不存在或不是普通文件。")
    return target.read_text(encoding="utf-8")


def list_files(path: str = ".") -> list[str]:
    """List one workspace directory, marking subdirectories with a slash."""
    target = resolve_workspace_path(path)
    names = []
    for entry in sorted(target.iterdir(), key=lambda item: item.name.casefold()):
        try:
            visible = resolve_workspace_path(entry.relative_to(resolve_workspace_path(".")).as_posix())
        except ValueError:
            continue
        if visible.is_dir():
            names.append(entry.name + "/")
        elif visible.is_file():
            names.append(entry.name)
    return names


def validate_search_arguments(query: str, max_results: int) -> None:
    if not isinstance(query, str) or not query.strip() or "\n" in query or "\r" in query:
        raise ValueError("query 必须是非空单行字符串。")
    if type(max_results) is not int or not 1 <= max_results <= 50:
        raise ValueError("max_results 必须是 1 至 50 的整数。")


def search_text(query: str, path: str = ".", max_results: int = 20) -> dict:
    """Find bounded literal line matches inside a visible workspace directory."""
    validate_search_arguments(query, max_results)
    target = resolve_workspace_path(path)
    if not target.is_dir():
        raise NotADirectoryError("搜索目录不存在或不是目录。")
    root = resolve_workspace_path(".")
    result = {"scope": "visible_workspace_text", "path": path, "query": query,
              "matches": [], "truncated": False, "skipped_files": 0, "skipped_directories": 0}
    directory_errors = []

    def visible(entry: Path) -> bool:
        try:
            resolved = resolve_workspace_path(entry.relative_to(root).as_posix())
            return resolved == entry and not entry.is_symlink()
        except (ValueError, OSError, RuntimeError):
            return False

    for directory, directories, files in os.walk(target, onerror=directory_errors.append, followlinks=False):
        current = Path(directory)
        directories[:] = sorted((name for name in directories
                                  if name.casefold() not in SEARCH_IGNORED_DIRS and visible(current / name)), key=str.casefold)
        for name in sorted(files, key=str.casefold):
            entry = current / name
            if not visible(entry) or not entry.is_file():
                continue
            try:
                with entry.open("rb") as stream:
                    content = stream.read(MAX_SEARCH_FILE_BYTES + 1)
                if len(content) > MAX_SEARCH_FILE_BYTES or b"\x00" in content:
                    result["skipped_files"] += 1
                    continue
                text = content.decode("utf-8")
            except (OSError, UnicodeError):
                result["skipped_files"] += 1
                continue
            for number, line in enumerate(text.splitlines(), 1):
                position = line.find(query)
                if position < 0:
                    continue
                if len(result["matches"]) == max_results:
                    return {**result, "truncated": True, "skipped_directories": len(directory_errors)}
                start = max(0, position - 60)
                excerpt = line[start:start + 200]
                result["matches"].append({"path": entry.relative_to(root).as_posix(), "line": number,
                                          "text": excerpt, "text_truncated": len(excerpt) != len(line)})
    result["skipped_directories"] = len(directory_errors)
    return result


def write_file(path: str, content: str) -> int:
    """Write a nonempty UTF-8 file inside an existing workspace directory."""
    target = resolve_write_path(path)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("写入内容必须是非空字符串。")
    return target.write_text(content, encoding="utf-8")


class EditConflict(Exception):
    """An edit cannot be safely applied to the current file contents."""


def prepare_edit(path: str, old_text: str, new_text: str) -> tuple[bytes, bytes, str]:
    """Prepare one literal replacement and a diff without changing the file."""
    target = resolve_write_path(path)
    if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
        raise ValueError("old_text 必须是非空字符串，new_text 必须是字符串。")
    original = target.read_bytes()
    text = original.decode("utf-8")
    # read_file normalizes line endings; accept LF or CRLF in the old text.
    pattern = r"\r?\n".join(re.escape(part) for part in old_text.replace("\r\n", "\n").split("\n"))
    matches = list(re.finditer(f"(?=({pattern}))", text))
    if not matches:
        raise EditConflict("old_text 未匹配到原文，未修改；请重新读取文件。")
    if len(matches) != 1:
        raise EditConflict("old_text 匹配到多处，未修改；请补充上下文以唯一定位。")
    match = matches[0]
    linebreak = re.search(r"\r?\n", match.group(1)) or re.search(r"\r?\n", text)
    newline = linebreak.group() if linebreak else "\n"
    replacement = new_text.replace("\r\n", "\n").replace("\n", newline)
    updated = text[:match.start(1)] + replacement + text[match.end(1):]
    if updated == text:
        raise EditConflict("新旧内容相同，未修改。")
    lines = difflib.unified_diff(text.splitlines(keepends=True), updated.splitlines(keepends=True),
                                 fromfile=f"a/{path}", tofile=f"b/{path}")
    diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
    return original, updated.encode("utf-8"), diff


def apply_edit(path: str, original: bytes, updated: bytes) -> None:
    """Save only if the file still matches the snapshot shown for approval."""
    target = resolve_write_path(path)
    if target.read_bytes() != original:
        raise EditConflict("预览后文件已变化，未覆盖；请重新读取并准备编辑。")
    target.write_bytes(updated)


def run_python(path: str) -> subprocess.CompletedProcess[str]:
    """Run a workspace script from its own directory with a timeout."""
    target = resolve_python_path(path)
    if not target.is_file():
        raise FileNotFoundError(f"{path} 不存在或不是普通文件。")
    child_env = os.environ.copy()
    child_env.pop("DEEPSEEK_API_KEY", None)
    return subprocess.run(
        [sys.executable, "-E", "-s", "-B", "-X", "utf8", str(target)],
        cwd=target.parent,
        env=child_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=RUN_TIMEOUT,
    )


@dataclass(frozen=True)
class PreparedTool:
    action: str
    execute: Callable[[], dict]


@dataclass(frozen=True)
class Tool:
    schema: dict
    validate: Callable[[dict], object]
    prepare: Callable[[dict], PreparedTool]
    read_only: bool = False

    def validate_arguments(self, arguments: dict) -> None:
        """Check fields first, then apply the tool's own value/path checks."""
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须是 JSON 对象。")
        parameters = self.schema["function"]["parameters"]
        required = set(parameters.get("required", []))
        if not required <= arguments.keys() or arguments.keys() - parameters["properties"].keys():
            raise ValueError(f"{self.schema['function']['name']} 参数字段不符合工具说明。")
        self.validate(arguments)


TOOL_REGISTRY: dict[str, Tool] = {}


def register_tool(tool: Tool) -> None:
    name = tool.schema["function"]["name"]
    if name in TOOL_REGISTRY:
        raise ValueError(f"工具 {name} 已注册。")
    TOOL_REGISTRY[name] = tool


def tool_schemas() -> list[dict]:
    return [tool.schema for tool in TOOL_REGISTRY.values()]


def _validate_search(arguments: dict) -> None:
    resolve_workspace_path(arguments.get("path", "."))
    validate_search_arguments(arguments["query"], arguments.get("max_results", 20))


def _validate_write(arguments: dict) -> None:
    resolve_write_path(arguments["path"])
    if not isinstance(arguments["content"], str) or not arguments["content"].strip():
        raise ValueError("写入内容必须是非空字符串。")


def _validate_edit(arguments: dict) -> None:
    resolve_write_path(arguments["path"])
    if not isinstance(arguments["old_text"], str) or not arguments["old_text"] or not isinstance(arguments["new_text"], str):
        raise ValueError("old_text 必须是非空字符串，new_text 必须是字符串。")


def _prepare_read(arguments: dict) -> PreparedTool:
    path = arguments["path"]
    return PreparedTool(f"读取 {path}", lambda: {"path": path, "content": read_file(path)})


def _prepare_list(arguments: dict) -> PreparedTool:
    path = arguments.get("path", ".")
    return PreparedTool(f"列出目录 {path}", lambda: {
        "scope": "visible_workspace_directory", "path": path, "files": list_files(path),
    })


def _prepare_search(arguments: dict) -> PreparedTool:
    action = f"搜索目录 {arguments.get('path', '.')} 中的 {arguments['query']!r}"
    return PreparedTool(action, lambda: search_text(**arguments))


def _prepare_write(arguments: dict) -> PreparedTool:
    path = arguments["path"]
    print(f"\n[即将写入 {path} 的完整内容]")
    print(arguments["content"])
    action = f"覆盖现有 {path}" if resolve_workspace_path(path).exists() else f"创建 {path}"
    return PreparedTool(action, lambda: {
        "path": path, "characters_written": write_file(path, arguments["content"]),
    })


def _prepare_edit(arguments: dict) -> PreparedTool:
    path = arguments["path"]
    original, updated, diff = prepare_edit(path, arguments["old_text"], arguments["new_text"])
    print(f"\n[即将编辑 {path} 的差异]")
    print(diff)

    def execute() -> dict:
        apply_edit(path, original, updated)
        return {"path": path, "replacements": 1, "diff": diff}

    return PreparedTool(f"按差异编辑 {path}", execute)


def _prepare_run(arguments: dict) -> PreparedTool:
    path = arguments["path"]

    def execute() -> dict:
        completed = run_python(path)
        return {"ok": completed.returncode == 0, "path": path, "exit_code": completed.returncode,
                "stdout": completed.stdout, "stderr": completed.stderr}

    return PreparedTool(f"运行 {path}（最长 {RUN_TIMEOUT} 秒）", execute)


TOOL_ERRORS = (EditConflict, subprocess.TimeoutExpired, OSError, UnicodeError)


def tool_error(tool: Tool, arguments: dict, error: Exception) -> dict:
    if isinstance(error, EditConflict):
        message = str(error)
    elif isinstance(error, subprocess.TimeoutExpired):
        message = f"运行超过 {RUN_TIMEOUT} 秒，Python 子进程已终止。"
    else:
        message = "工具执行失败，请检查文件是否存在、访问权限及 UTF-8 编码。"
    result = {"ok": False, "tool": tool.schema["function"]["name"],
              "error": {"type": type(error).__name__, "message": message}}
    if "path" in tool.schema["function"]["parameters"]["properties"]:
        result["path"] = arguments.get("path", ".")
    return result


register_tool(Tool(
    schema={
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取 workspace 内普通 UTF-8 文件的完整内容。path 必须是相对 workspace 的路径，例如 demo_project/src/stats.py；不读取点号开头的文件或目录。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对 workspace 的文件路径"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    validate=lambda args: resolve_workspace_path(args["path"]),
    prepare=_prepare_read,
    read_only=True,
))


register_tool(Tool(
    schema={
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出 workspace 内指定目录的可访问条目，不读取正文。path 默认是根目录 .；目录名以 / 结尾，子目录需继续列出。点号开头的项和越界链接不展示，未列出不代表不存在。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对 workspace 的目录路径，省略时为 ."}},
                "additionalProperties": False,
            },
        },
    },
    validate=lambda args: resolve_workspace_path(args.get("path", ".")),
    prepare=_prepare_list,
    read_only=True,
))


register_tool(Tool(
    schema={
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "在 workspace 内指定目录递归搜索普通 UTF-8 文件中的单行关键词，区分大小写、按字面匹配。返回相对路径、1 起始行号和至多 200 字符的行摘要。path 默认 .，max_results 默认 20、范围 1 至 50。跳过点号开头的项、链接、常见依赖与构建目录、超过 256 KiB 或无法读取的文本；truncated 表示还有命中未返回。无匹配只覆盖此过滤范围，定位后用 read_file 核对完整源码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "非空单行关键词，不是正则表达式"},
                    "path": {"type": "string", "description": "相对 workspace 的搜索目录，省略时为 ."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "description": "最多返回的命中行数，默认 20"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    validate=_validate_search,
    prepare=_prepare_search,
    read_only=True,
))


register_tool(Tool(
    schema={
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "在 workspace 内创建或覆盖完整 UTF-8 文件，父目录必须已存在；路径限制与读取相同。project.txt、next_step.txt、test_main.py 和 demo_project/test_stats.py 只读。写入前需用户确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对 workspace 的文件路径，例如 demo_project/src/stats.py"},
                    "content": {"type": "string", "description": "完整的文件内容"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    validate=_validate_write,
    prepare=_prepare_write,
    read_only=False,
))


register_tool(Tool(
    schema={
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "精确替换 workspace 中现有 UTF-8 文件的一处内容。先读取文件，old_text 必须恰好匹配一处；展示差异并经用户确认后写入。匹配失败或确认期间文件变化时不写入。与 write_file 共用只读与路径限制。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对 workspace 的文件路径"},
                    "old_text": {"type": "string", "description": "需要替换的原文，包含必要上下文以唯一定位；不可为空"},
                    "new_text": {"type": "string", "description": "替换后的内容；空字符串表示删除原文"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
    validate=_validate_edit,
    prepare=_prepare_edit,
    read_only=False,
))


register_tool(Tool(
    schema={
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "经用户确认后运行 workspace 内的普通 .py 文件，以脚本所在目录为工作目录，支持导入同项目模块；返回退出码、标准输出和标准错误，最长运行 10 秒。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对 workspace 的 .py 文件路径，例如 demo_project/main.py"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    validate=lambda args: resolve_python_path(args["path"]),
    prepare=_prepare_run,
    read_only=False,
))
