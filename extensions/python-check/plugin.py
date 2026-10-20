"""A single-file plugin; target Python code is parsed, never executed."""

import ast

from learn_agent.permissions import resolve_workspace_path


def execute(tool_name, arguments, workspace):
    if tool_name != "inspect_python":
        raise ValueError("未知插件工具。")
    target = resolve_workspace_path(arguments["path"])
    if target.suffix.lower() != ".py" or not target.is_file():
        raise ValueError("请选择 workspace 内的普通 Python 文件。")
    with target.open("rb") as stream:
        raw = stream.read(131073)
    if len(raw) > 131072:
        raise ValueError("目标 Python 文件超过 128 KiB。")
    tree = ast.parse(raw.decode("utf-8-sig"), filename=str(target))
    definitions = [{"name": node.name, "kind": type(node).__name__, "line": node.lineno}
                   for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    definitions.sort(key=lambda item: item["line"])
    return {"path": target.relative_to(workspace).as_posix(), "analysis": "static_ast",
            "definitions": definitions, "definition_count": len(definitions), "source_executed": False}
