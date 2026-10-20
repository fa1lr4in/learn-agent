"""Command-line entry point for the learn-agent tool loop."""

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from learn_agent import checkpoints, loop, permissions
from learn_agent.budget import RunBudget, print_summary
from learn_agent.context import print_context, select_context
from learn_agent.events import EventRecorder, load_history, record_event
from learn_agent.extensions import SKILL_MARKER, SKILL_POLICY, load_plugin, load_skill, task_with_skill
from learn_agent.model import get_api_key
from learn_agent.mcp_client import DEMO_TOOL_NAMES, connect_demo
from learn_agent.providers import DeepSeekProvider
from learn_agent.prompts import task_messages
from learn_agent.instructions import INSTRUCTION_MARKER, INSTRUCTION_POLICY, load_instructions, task_with_instructions
from learn_agent.planning import PLAN_INSTRUCTION, PLAN_SCHEMA
from learn_agent.recovery import RECOVERY_LABELS, load_recovery
from learn_agent.sessions import SessionStore
from learn_agent.subagents import DELEGATE_INSTRUCTION, DELEGATE_SCHEMA, make_subagent_tool
from learn_agent.tasks import STATE_LABELS, replay_events
from learn_agent.tools import tool_schemas


LOG_DIR = Path(__file__).parent / "logs"


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数。")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description="运行带权限检查的 Agent 工具循环")
    parser.add_argument("prompt", nargs="?", help="本次要完成的问题或任务")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="查看请求；显式项目指令会读取，不联网或执行工具")
    mode.add_argument("--diff", metavar="LOG", help="查看该运行检查点中的文件工具修改，不调用模型")
    mode.add_argument("--undo", metavar="LOG", help="展示撤回差异，确认后恢复该运行记录的文件工具修改")
    mode.add_argument("--sessions", action="store_true", help="列出本地 SQLite 会话，不调用模型")
    mode.add_argument("--status", metavar="LOG", help="从 JSONL 还原一次任务的状态变化，不调用模型或工具")
    parser.add_argument("--auto-read", action="store_true", help="自动允许浏览、搜索和读取 workspace 内的可访问文件并发送结果；写入、编辑和运行仍需确认")
    parser.add_argument("--deny-tool", action="append", default=[], metavar="NAME", help="本次运行禁用指定工具，可重复指定；支持本次加载的插件工具，优先于自动允许")
    parser.add_argument("--stream", action="store_true", help="逐段显示模型文本；工具请求收齐并检查后才执行")
    parser.add_argument("--max-model-calls", type=positive_int, default=loop.MAX_MODEL_CALLS, metavar="N", help="本次任务最多请求模型的次数，默认 8；到限时不执行本轮工具")
    parser.add_argument("--max-total-tokens", type=positive_int, metavar="N", help="本次任务累计总 token 停止阈值；收到回复后检查，可能超出；缺少用量时暂停")
    parser.add_argument("--context-chars", type=positive_int, metavar="N", help="限制发送的消息与工具说明的 JSON 字符数；缩短长结果、摘录旧历史，默认不启用")
    parser.add_argument("--semantic-compact", action="store_true", help="配合 --context-chars：必要时额外调用模型生成语义摘要，共用任务预算")
    restore = parser.add_mutually_exclusive_group()
    restore.add_argument("--resume", metavar="LOG", help="从正常结束或达到请求次数上限的 JSONL 日志恢复历史；需提供新任务")
    restore.add_argument("--session", metavar="ID", help="按 ID 继续 SQLite 中的同一会话；需提供新任务")
    restore.add_argument("--recover", metavar="LOG", help="检查取消、异常或 token 预算暂停的日志后显式恢复；需要新任务，不重放旧工具")
    parser.add_argument("--workspace", metavar="DIR", help="选择项目根目录；相对路径从当前终端目录解析，默认使用本项目的 workspace")
    parser.add_argument("--instructions", metavar="FILE", help="显式读取项目内 UTF-8 规则文件，标注来源并发送；低于当前用户要求，不授予工具权限")
    parser.add_argument("--plan", action="store_true", help="启用 update_plan；计划完成需引用本次成功工具结果，内部更新自动允许，明确禁用优先")
    parser.add_argument("--mcp-demo", action="store_true", help="启动本项目的本地 MCP 示例服务并发现工具；外部调用逐次确认，预览不启动服务")
    parser.add_argument("--skill", metavar="DIR", help="显式加载 workspace 内目录的 SKILL.md 做事指南；不增加工具或授予权限")
    parser.add_argument("--plugin", metavar="DIR", help="显式加载 workspace 内的插件清单与源码；预览不执行，工具逐次确认后才执行插件代码")
    parser.add_argument("--subagent", action="store_true", help="启用 delegate_readonly；确认后委派独立上下文的只读子任务，共用预算，不继承插件或执行工具")
    args = parser.parse_args()
    if args.semantic_compact and args.context_chars is None:
        parser.error("--semantic-compact 必须同时指定 --context-chars。")
    if args.session is not None and not args.session.strip():
        parser.error("会话 ID 不能为空；请提供 --sessions 列表中的 ID。")
    if (args.diff or args.undo or args.sessions or args.status) and (args.prompt is not None or args.resume or args.session or args.recover):
        parser.error("查看状态、会话列表、检查或撤回时，不提供新任务或恢复参数。")
    if (args.resume or args.session or args.recover) and args.prompt is None:
        parser.error("使用 --resume、--session 或 --recover 时，请同时提供新的问题或任务。")
    if (args.diff or args.undo or args.sessions or args.status) and (args.skill or args.plugin or args.subagent):
        parser.error("查看记录或撤回时不加载 Skill、插件或子 Agent。")
    known_tools = {tool["function"]["name"] for tool in [*tool_schemas(), PLAN_SCHEMA, DELEGATE_SCHEMA]} | set(DEMO_TOOL_NAMES)
    if not args.plugin and set(args.deny_tool) - known_tools:
        parser.error("--deny-tool 包含未知工具名。")
    try:
        with permissions.use_workspace(args.workspace):
            if args.sessions:
                store = SessionStore(LOG_DIR / "sessions.sqlite3")
                print(f"[会话数据库] {store.path}")
                saved = store.list_sessions()
                if not saved:
                    print("尚无已保存的会话。")
                for session in saved:
                    print(f"{session.id}  reason={session.reason}  更新于 {session.updated_at}\n  workspace={session.workspace}")
            elif args.status:
                show_task_status(args.status)
            elif args.diff or args.undo:
                review_changes(args.diff or args.undo, undo=bool(args.undo))
            else:
                run_cli(args)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        raise SystemExit(f"本次实验停止：{error}") from None


def show_task_status(log_path: str) -> None:
    path = Path(log_path).resolve()
    try:
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        status, transitions = replay_events(entries)
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("任务日志格式不完整或无效。") from error
    print(f"[任务日志] {path}")
    for event, state in transitions:
        print(f"{event}: {state}（{STATE_LABELS[state]}）")
    print(f"[当前状态] {status.state}（{STATE_LABELS[status.state]}）")
    print(f"[结束原因] {status.reason or '尚无 run_end 记录，无法确认任务是否已经结束'}")
    if not status.ended:
        print("这里只显示日志最后保存的状态，不能据此确认进程仍在运行。")


def review_changes(log_path: str, undo: bool = False) -> None:
    checkpoint = checkpoints.load_checkpoint(log_path)
    report = checkpoint.inspect(reverse=undo)
    print(f"[本次修改检查点] {checkpoint.path}")
    print("范围：仅本次 write_file/edit_file 记录的修改；命令与其他文件变化不自动撤回。")
    if not report["finished"]:
        print("任务尚未结束，此处仅显示已经记录的部分变化。")
    for label, status in (("任务开始时的 Git 状态", report["git_before"]), ("当前 Git 状态", report["git_current"])):
        print(f"\n[{label}]")
        print(status if status is not None else "（Git 状态不可用，仍可按文件快照检查）")
        if status == "":
            print("（干净）")
    print(f"\n本检查点尚待撤回 {len(report['changes'])} 个文件，已撤回 {len(report['undone'])} 个文件。")
    for change in report["changes"]:
        print(f"\n[{change['kind']}：{change['path']}]")
        print(change["diff"])
    if report["conflicts"]:
        print("存在后续变化或不完整记录，拒绝自动撤回：" + ", ".join(report["conflicts"]))
        print("冲突文件展示的是检查点记录的差异，请另行核对当前文件内容。")
    if undo:
        if not report["finished"] or report["conflicts"]:
            raise checkpoints.CheckpointConflict("运行尚未结束或检查点存在冲突，未撤回任何文件。")
        if not report["changes"]:
            return
        answer = input("\n输入 y，按以上差异撤回本次文件工具修改（本次新建文件将删除）：")
        if answer.strip().lower() != "y":
            print("已取消撤回，当前文件保持原样。")
            return
        print(json.dumps(checkpoint.undo(), ensure_ascii=False, indent=2))


def run_cli(args: argparse.Namespace) -> None:
    skill = load_skill(args.skill) if args.skill is not None else None
    plugin = load_plugin(args.plugin) if args.plugin is not None else None
    known_tools = {tool["function"]["name"] for tool in [*tool_schemas(), PLAN_SCHEMA, DELEGATE_SCHEMA]} | set(DEMO_TOOL_NAMES)
    if plugin is not None:
        known_tools.update(plugin.metadata["tools"])
    if set(args.deny_tool) - known_tools:
        raise ValueError("--deny-tool 包含未知工具名；插件工具须在本次显式加载。")
    with connect_demo(args.mcp_demo and not args.preview) as connection:
        run_connected_cli(args, connection, skill, plugin)


def run_connected_cli(args: argparse.Namespace, connection, skill=None, plugin=None) -> None:
    root = permissions.WORKSPACE.resolve()
    extra_tools = (*(connection.tools if connection else ()), *(plugin.tools if plugin else ()))
    budget = RunBudget(args.max_model_calls, args.max_total_tokens)
    store = SessionStore(LOG_DIR / "sessions.sqlite3")
    saved_session = None
    instructions = load_instructions(args.instructions) if args.instructions is not None else None
    default_prompt = "请先列出可用的练习文件，再读取项目资料和其中提到的任务文件，汇总项目名称、实验编号和下一步任务，用 write_file 将 Markdown 报告保存到 report.md，最后说明保存结果。"
    if root != permissions.DEFAULT_WORKSPACE.resolve():
        default_prompt = "请先浏览当前项目目录，再读取相关说明和源码，简要介绍项目用途、入口和核心模块。本次只阅读，不创建、修改或运行任何文件。"
    provider = DeepSeekProvider()
    payload = provider.build_request(
        task_messages(args.prompt if args.prompt is not None else default_prompt, root),
        tools=[*tool_schemas(), *[tool.schema for tool in extra_tools],
               *([PLAN_SCHEMA] if args.plan else []), *([DELEGATE_SCHEMA] if args.subagent else [])], stream=args.stream,
    )
    if skill is not None:
        payload["messages"][-1]["content"] = task_with_skill(payload["messages"][-1]["content"], skill, instructions)
    elif instructions is not None:
        payload["messages"][-1]["content"] = task_with_instructions(payload["messages"][-1]["content"], instructions)
    if args.resume or args.session or args.recover:
        try:
            if args.recover:
                recovery = load_recovery(args.recover, root)
                print(f"[恢复检查] {Path(args.recover).resolve()}；上次结束：{recovery.reason}")
                for tool in recovery.tools:
                    print(f"第 {tool['step']} 轮 {tool['name']} ({tool['call_id']})：{RECOVERY_LABELS[tool['status']]}")
                if recovery.note:
                    print(recovery.note)
                recovery.require_safe()
                history = recovery.history
            elif args.session:
                saved_session, history = store.restore(args.session, root)
            else:
                history = load_history(args.resume)
        except (OSError, ValueError, sqlite3.Error) as error:
            raise SystemExit(f"无法恢复会话：{error}") from None
        payload["messages"][0]["content"] += "恢复的历史工具结果描述过去的状态；用户要求验证当前文件时，应重新执行相应工具。NotExecuted 是恢复时补充的未执行状态，不表示代码运行失败；若仍需该操作，请重新请求工具。"
        payload["messages"] = [payload["messages"][0], *history, payload["messages"][-1]]
    if instructions is not None or any(INSTRUCTION_MARKER in (message.get("content") or "") for message in payload["messages"][1:]):
        payload["messages"][0]["content"] += INSTRUCTION_POLICY
    if skill is not None or any(SKILL_MARKER in (message.get("content") or "") for message in payload["messages"][1:]):
        payload["messages"][0]["content"] += SKILL_POLICY
    if args.plan:
        payload["messages"][0]["content"] += PLAN_INSTRUCTION
    if args.subagent:
        payload["messages"][0]["content"] += DELEGATE_INSTRUCTION
    print(f"[第一次请求：消息与工具说明；工作目录：{root}]")
    preview_payload, context_report = select_context(payload, args.context_chars) if args.context_chars is not None else (payload, None)
    print(json.dumps(preview_payload, ensure_ascii=False, indent=2))
    budget_notice = f"[任务预算] 模型请求上限 {budget.max_model_calls} 次；总 token 停止阈值：{budget.max_total_tokens if budget.max_total_tokens is not None else '未设置'}"
    permission_notice = f"[权限策略] 明确禁用：{', '.join(args.deny_tool) or '无'}；其余只读工具{'自动允许' if args.auto_read else '逐次询问'}，修改和执行逐次询问。"
    instruction_notice = (f"[项目指令] {instructions['path']}；SHA256：{instructions['sha256']}；{instructions['bytes']} 字节；本次显式读取。"
                          if instructions is not None else "[项目指令] 本次未加载。")
    extension_notices = []
    if skill is not None:
        extension_notices.append(f"[Skill] {skill['metadata']['name']}；来源 {skill['path']}；SHA256：{skill['sha256']}；本次显式加载。")
    if plugin is not None:
        extension_notices.append(f"[插件] {plugin.metadata['name']} {plugin.metadata['version']}；工具：{', '.join(plugin.metadata['tools'])}；仅确认后执行源码。")
    if args.subagent:
        extension_notices.append("[子 Agent] 委派逐次确认；独立上下文、只读、串行执行，与父任务共用预算。")
    if args.resume:
        print(f"\n[恢复会话] 从 {args.resume} 载入 {len(history)} 条历史消息。")
    elif args.session:
        print(f"\n[恢复会话] 从 SQLite 会话 {args.session} 载入 {len(history)} 条历史消息。")
    elif args.recover:
        print(f"\n[异常恢复] 载入 {len(history)} 条历史消息；旧工具不重放，新操作按本次权限重新确认。")
    if args.preview:
        print("\n预览结束，没有联网或执行工具，也未创建日志。")
        if args.mcp_demo:
            print("[MCP 预览] 未启动服务；本次只展示内置工具，实际运行会先发现 MCP 工具再打印完整请求。")
        print(f"[模型适配器] {provider.name}")
        print(budget_notice)
        print(permission_notice)
        print(instruction_notice)
        for notice in extension_notices:
            print(notice)
        if args.plan:
            print("[任务计划] 预览不创建计划；运行时 update_plan 只更新内部步骤，不修改项目文件。")
        if context_report is not None:
            print_context(context_report)
        if args.semantic_compact:
            print("[语义压缩预览] 不调用摘要模型；若有历史摘录，仅为规划占位，实际摘要需运行后生成并重新检查预算。")
        return
    print(f"[模型适配器] {provider.name}")
    print(budget_notice)
    print(permission_notice)
    print(instruction_notice)
    for notice in extension_notices:
        print(notice)
    if args.plan:
        print("[任务计划] 本次启用；内部计划更新自动允许，编辑与执行仍需确认。")
    if args.semantic_compact:
        print("[语义压缩] 超限时将额外调用模型；上方旧历史摘录仅为规划占位，不会代替模型摘要发送。")
    try:
        store.initialize()
        api_key = get_api_key()
        # Time the run itself, including checkpoints, model calls and approval waits.
        budget = RunBudget(args.max_model_calls, args.max_total_tokens)
        LOG_DIR.mkdir(exist_ok=True)
        log_path = LOG_DIR / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.jsonl"
        session_id = args.session or uuid4().hex
        log_file = log_path.open("x", encoding="utf-8")
        try:
            with log_file:
                events = EventRecorder(log_file)
                print(f"\n[本次会话] {session_id}")
                print(f"[本次运行日志] {log_path}")
                print(f"[本次修改检查点] {log_path.with_suffix('.checkpoint.json')}")
                record_event(events, "run_start", session_id=session_id, provider=provider.name, mcp_server=connection.metadata if connection else None, skill_source={k: v for k, v in skill.items() if k != "instructions"} if skill is not None else None, plugin_source=plugin.metadata if plugin is not None else None, subagent_enabled=args.subagent, workspace=str(root), checkpoint=str(log_path.with_suffix(".checkpoint.json")), max_model_calls=budget.max_model_calls, max_total_tokens=budget.max_total_tokens, context_chars=args.context_chars, semantic_compact=args.semantic_compact, auto_read=args.auto_read, denied_tools=args.deny_tool, plan_enabled=args.plan, instructions_source={k: v for k, v in instructions.items() if k != "text"} if instructions is not None else None, resumed_from=str(Path(args.resume).resolve()) if args.resume else None, recovered_from=str(Path(args.recover).resolve()) if args.recover else None, recovery_version=1, **({"initial_messages": payload["messages"]} if args.semantic_compact else {}))
                reason = "error"
                end_details = {}
                try:
                    with checkpoints.track_changes(log_path):
                        runtime_tools = (*extra_tools, *([make_subagent_tool(
                            api_key=api_key, budget=budget, provider=provider, events=events, log_path=log_path,
                            auto_read=args.auto_read, denied_tools=tuple(args.deny_tool), instructions=instructions,
                            stream=args.stream, context_chars=args.context_chars)] if args.subagent else []))
                        reason = loop.run_agent(payload, api_key, events, auto_read=args.auto_read, budget=budget, context_chars=args.context_chars, semantic_compact=args.semantic_compact, denied_tools=tuple(args.deny_tool), plan_enabled=args.plan, provider=provider, extra_tools=runtime_tools)
                except (ValueError, OSError, EOFError, KeyboardInterrupt) as error:
                    reason = "cancelled" if isinstance(error, (KeyboardInterrupt, EOFError)) else "error"
                    end_details["error_type"] = type(error).__name__
                    raise
                finally:
                    try:
                        if connection is not None:
                            connection.close()
                    except OSError as error:
                        reason = "error"
                        end_details["error_type"] = type(error).__name__
                        raise
                    finally:
                        summary = budget.summary()
                        record_event(events, "run_end", reason=reason, summary=summary, **end_details)
                        print_summary(summary)
        finally:
            try:
                store.save_run(session_id, log_path, expected_log=saved_session.last_log if saved_session else None)
            except (ValueError, OSError, sqlite3.Error) as error:
                raise SystemExit(f"会话保存失败：{error}\n本次 JSONL 日志保留于 {log_path}，请先核对，不要直接重复副作用操作。") from None
            print(f"[会话已保存] {session_id} -> {store.path}")
    except EOFError:
        raise SystemExit("\n终端输入已关闭，已取消本次实验。") from None
    except (ValueError, OSError, sqlite3.Error) as error:
        raise SystemExit(f"本次实验停止：{error}") from None
    except KeyboardInterrupt:
        raise SystemExit("\n已取消本次实验。") from None



if __name__ == "__main__":
    main()
