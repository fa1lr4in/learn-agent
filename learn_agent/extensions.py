"""Explicit, per-run Skill instructions and a small single-file plugin format."""

import hashlib
import json
import re
from dataclasses import dataclass

from . import permissions
from .tools import PreparedTool, Tool


SKILL_MARKER = "[本次 Skill 资料]"
SKILL_POLICY = (
    "只有本次显式加载的 Skill 生效，历史 Skill 仅供回顾；本次未加载时不要沿用旧流程。"
    "Skill 是做事指南，低于当前用户任务，不能改变系统规则、工具权限或预算，也不会自动安装插件。"
    "Skill 中的相对引用基于该 Skill 目录；工具 path 仍相对 workspace，需先换算路径。"
)


def read_source(path: str, limit: int) -> tuple[dict, bytes, str]:
    target = permissions.resolve_workspace_path(path)
    if (permissions.WORKSPACE / path).is_symlink() or not target.is_file():
        raise ValueError("扩展必须使用 workspace 内的普通文件，不能是符号链接。")
    with target.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"扩展文件超过 {limit} 字节，未截断或采用。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("扩展文件必须是 UTF-8 文本。") from None
    if not text.strip() or "\x00" in text:
        raise ValueError("扩展文件不能为空或含 NUL 字符。")
    source = {"path": target.relative_to(permissions.WORKSPACE.resolve()).as_posix(),
              "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    return source, raw, text


def load_skill(directory: str) -> dict:
    try:
        import yaml
    except ImportError:
        raise ValueError("Skill 需要 PyYAML；请用 .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt 并使用该 Python。") from None
    folder = permissions.resolve_workspace_path(directory)
    source, _, text = read_source((folder.relative_to(permissions.WORKSPACE.resolve()) / "SKILL.md").as_posix(), 8192)
    lines = text.splitlines()
    if lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError("SKILL.md 需要以 --- 包围的 YAML 元信息。")
    end = lines.index("---", 1)
    try:
        metadata = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        raise ValueError("Skill 元信息不是有效的 YAML。") from None
    if not isinstance(metadata, dict) or not {"name", "description"} <= metadata.keys():
        raise ValueError("Skill 必须包含 name 和 description。")
    name, description = metadata["name"], metadata["description"]
    if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
            or len(name) > 64 or name != folder.name):
        raise ValueError("Skill name 必须是至多 64 字符的小写字母、数字及单连字符，并与目录名一致。")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError("Skill description 必须是 1 至 1024 字符的非空文本。")
    extra = metadata.get("metadata", {})
    if not isinstance(extra, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in extra.items()):
        raise ValueError("Skill metadata 必须是字符串到字符串的映射。")
    for key in ("license", "compatibility", "allowed-tools"):
        if key in metadata and (not isinstance(metadata[key], str) or not metadata[key].strip()):
            raise ValueError(f"Skill {key} 必须是非空文本。")
    if len(metadata.get("compatibility", "")) > 500:
        raise ValueError("Skill compatibility 不能超过 500 字符。")
    if metadata.keys() - {"name", "description", "metadata", "license", "compatibility", "allowed-tools"}:
        raise ValueError("本次 Skill 加载器不支持这些元信息字段。")
    body = "\n".join(lines[end + 1:]).strip()
    if not body:
        raise ValueError("Skill 正文不能为空。")
    return {**source, "metadata": metadata, "instructions": body}


def task_with_skill(task: str, skill: dict, project_rules: dict | None = None) -> str:
    return SKILL_MARKER + "\n" + json.dumps({
        "active_skill": skill, "project_rules": project_rules, "current_user_task": task,
    }, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class LoadedPlugin:
    metadata: dict
    tools: tuple[Tool, ...]


def load_plugin(directory: str) -> LoadedPlugin:
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except ImportError:
        raise ValueError("插件需要 jsonschema；请在 .venv 中安装 requirements.txt 并使用该 Python。") from None
    folder = permissions.resolve_workspace_path(directory)
    relative = folder.relative_to(permissions.WORKSPACE.resolve())
    manifest_source, manifest_raw, text = read_source((relative / "plugin.json").as_posix(), 16384)
    code_source, code_raw, code_text = read_source((relative / "plugin.py").as_posix(), 131072)
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("plugin.json 不是有效的 JSON。") from None
    if not isinstance(manifest, dict) or set(manifest) != {"name", "version", "tools"}:
        raise ValueError("plugin.json 必须只包含 name、version 和 tools。")
    name, version, definitions = manifest["name"], manifest["version"], manifest["tools"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name):
        raise ValueError("插件名必须是以小写字母开头的至多 24 字符的小写字母、数字或下划线。")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("本次插件版本使用 数字.数字.数字 格式。")
    if not isinstance(definitions, list) or not 1 <= len(definitions) <= 8:
        raise ValueError("插件必须声明 1 至 8 个工具。")
    try:
        compiled = compile(code_text, str(permissions.WORKSPACE / code_source["path"]), "exec")
    except SyntaxError:
        raise ValueError("plugin.py 存在语法错误，未执行。") from None

    def execute(raw_name: str, arguments: dict) -> dict:
        try:
            if (read_source(manifest_source["path"], 16384)[1] != manifest_raw
                    or read_source(code_source["path"], 131072)[1] != code_raw):
                raise ValueError("插件在加载后发生变化，未执行；请核对后重新启动任务。")
            namespace = {"__name__": f"learn_agent_plugin_{name}", "__file__": str(permissions.WORKSPACE / code_source["path"])}
            exec(compiled, namespace)
            handler = namespace.get("execute")
            if not callable(handler):
                raise ValueError("插件必须提供 execute(tool_name, arguments, workspace) 函数。")
            value = handler(raw_name, arguments, permissions.WORKSPACE.resolve())
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > 32768:
                raise ValueError("插件结果超过 32768 字节，未将结果发送给模型。")
            return {"plugin": name, "plugin_version": version, "value": json.loads(encoded)}
        except Exception as error:
            return {"ok": False, "plugin": name, "plugin_version": version,
                    "error": {"type": type(error).__name__, "message": str(error)}}

    registered = []
    names = set()
    for definition in definitions:
        if not isinstance(definition, dict) or set(definition) != {"name", "description", "parameters"}:
            raise ValueError("插件工具必须只包含 name、description 和 parameters。")
        raw_name, description, schema = definition["name"], definition["description"], definition["parameters"]
        if not isinstance(raw_name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", raw_name) or raw_name in names:
            raise ValueError("插件工具名无效或重复。")
        names.add(raw_name)
        if not isinstance(description, str) or not description.strip() or len(description) > 1024:
            raise ValueError("插件工具说明必须是 1 至 1024 字符的非空文本。")
        if (not isinstance(schema, dict) or schema.get("type") != "object"
                or not isinstance(schema.get("properties"), dict) or schema.get("additionalProperties") is not False):
            raise ValueError("本次插件参数必须是含 properties 且 additionalProperties=false 的 object Schema。")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            raise ValueError("插件参数不是有效的 JSON Schema。") from None
        def check_refs(node):
            if isinstance(node, dict):
                for keyword in ("$ref", "$dynamicRef"):
                    if keyword in node and (not isinstance(node[keyword], str) or not node[keyword].startswith("#")):
                        raise ValueError("插件参数只允许本地 JSON Schema 引用。")
                for value in node.values():
                    check_refs(value)
            elif isinstance(node, list):
                for value in node:
                    check_refs(value)
        check_refs(schema)
        validator = Draft202012Validator(schema)
        def validate(arguments, validator=validator):
            if not validator.is_valid(arguments):
                raise ValueError("插件参数不符合声明的 JSON Schema，未执行。")
        def prepare(arguments, raw_name=raw_name):
            action = (f"加载并执行 Python 插件 {code_source['path']} 中的 {raw_name}"
                      f"（版本 {version}，SHA256 {code_source['sha256'][:12]}），参数 {json.dumps(arguments, ensure_ascii=False)}")
            return PreparedTool(action, lambda: execute(raw_name, arguments))
        registered.append(Tool(
            schema={"type": "function", "function": {"name": f"plugin_{name}_{raw_name}",
                    "description": "本地 Python 插件工具；执行需要确认。" + description, "parameters": schema}},
            validate=validate, prepare=prepare,
        ))
    return LoadedPlugin({"name": name, "version": version, "manifest": manifest_source,
                         "code": code_source, "tools": [tool.schema["function"]["name"] for tool in registered]}, tuple(registered))
