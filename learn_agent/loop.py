"""Bounded model/tool loop, independent of individual tool names."""

import json
from typing import TextIO

from . import tools
from .budget import RunBudget
from .compaction import generate_summary
from .context import ContextLimitError, print_context, select_context
from .events import EventRecorder, record_event
from .providers import DeepSeekProvider, ModelProvider
from .permissions import PermissionPolicy, approve_tool
from .planning import TaskPlan


MAX_MODEL_CALLS = 8


def run_agent(payload: dict, api_key: str, log_file: TextIO | EventRecorder | None = None, auto_read: bool = False,
              budget: RunBudget | None = None, context_chars: int | None = None, semantic_compact: bool = False,
              denied_tools: tuple[str, ...] = (), plan_enabled: bool = False,
              provider: ModelProvider | None = None, extra_tools: tuple[tools.Tool, ...] = (),
              tool_names: tuple[str, ...] | None = None, max_steps: int | None = None,
              call_purpose: str = "task") -> str:
    if not isinstance(log_file, EventRecorder):
        log_file = EventRecorder(log_file)
    budget = budget if budget is not None else RunBudget(MAX_MODEL_CALLS)
    provider = provider if provider is not None else DeepSeekProvider()
    registry = dict(tools.TOOL_REGISTRY) if tool_names is None else {name: tools.TOOL_REGISTRY[name] for name in tool_names}
    for tool in extra_tools:
        name = tool.schema["function"]["name"]
        if name in registry or name == "update_plan":
            raise ValueError(f"外部工具 {name} 与已有工具重名，未请求模型。")
        registry[name] = tool
    plan = TaskPlan(log_file) if plan_enabled else None
    if plan is not None:
        plan_tool = plan.as_tool()
        registry[plan_tool.schema["function"]["name"]] = plan_tool
    request = {**payload, "tools": [tool.schema for tool in registry.values()], "tool_choice": "auto"}
    summary_cache = None
    policy = PermissionPolicy(auto_read, denied_tools)
    step_limit = max_steps if max_steps is not None else budget.max_model_calls
    for step in range(1, step_limit + 1):
        reason = budget.pause_reason() or ("max_model_calls" if budget.model_calls >= budget.max_model_calls else None)
        if reason:
            record_event(log_file, "budget_paused", reason=reason, full_messages=request["messages"])
            print(f"任务预算已停止（{reason}），未请求下一轮模型。")
            return reason
        if plan is not None:
            system = {**payload["messages"][0], "content": payload["messages"][0]["content"] + plan.context()}
            request = {**request, "messages": [system, *request["messages"][1:]]}
        api_request = request
        context_details = {}
        if context_chars is not None:
            try:
                api_request, report = select_context(request, context_chars, summary_cache)
                if semantic_compact and report["needs_summary"]:
                    summary_cache = generate_summary(request, report, summary_cache, api_key, budget, log_file, step, provider)
                    reason = budget.pause_reason() or ("max_model_calls" if budget.model_calls >= budget.max_model_calls else None)
                    if reason:
                        if reason == "max_model_calls":
                            record_event(log_file, "budget_paused", reason=reason, full_messages=request["messages"])
                        print(f"摘要调用后任务预算已停止（{reason}）；未请求本轮任务模型，已有结果保留。")
                        return reason
                    api_request, report = select_context(request, context_chars, summary_cache)
                    if report["needs_summary"]:
                        record_event(log_file, "context_selected", step=step,
                                     **{**report, "blocked": True, "blocked_reason": "summary_did_not_fit"})
                        print("模型摘要未能在本次选择范围内满足字符预算；请提高上限后恢复，未继续请求模型。")
                        return "context_limit"
            except ContextLimitError as error:
                record_event(log_file, "context_selected", step=step, **error.report)
                print(f"任务暂停：{error} 未请求本轮模型，此前已完成的操作保留。")
                return "context_limit"
            print_context(report)
            record_event(log_file, "context_selected", step=step, **report)
            context_details["full_messages"] = request["messages"]
        if step > 1:
            if request.get("stream"):
                print(f"\n[第 {step} 次请求：携带历史消息继续流式接收]")
            else:
                print(f"\n[第 {step} 次请求里的消息历史]")
                print(json.dumps(api_request["messages"], ensure_ascii=False, indent=2))
        budget.start_call(call_purpose)
        record_event(log_file, "model_request", step=step, call_index=budget.model_calls, payload=api_request, **context_details)
        reply_label = f"第 {step} 轮任务回复；总第 {budget.model_calls} 次模型请求" if semantic_compact else f"第 {step} 次模型回复"
        if call_purpose == "subagent":
            reply_label = f"子 Agent 第 {step} 次模型回复"
        if request.get("stream"):
            print(f"\n[{reply_label}：流式接收]", flush=True)
            tool_notice = False

            def show_delta(delta):
                nonlocal tool_notice
                record_event(log_file, "model_delta", step=step, delta=delta)
                if delta.get("content"):
                    print(delta["content"], end="", flush=True)
                if delta.get("tool_calls") and not tool_notice:
                    print("\n[工具请求接收中，参数收齐并检查后才会执行]", flush=True)
                    tool_notice = True

            try:
                response = provider.complete(api_request, api_key, on_delta=show_delta)
            finally:
                print(flush=True)
        else:
            response = provider.complete(api_request, api_key)
        record_event(log_file, "model_response", step=step, response=response.as_dict())
        budget.observe(response.as_dict(), call_purpose)
        message = response.message
        if not request.get("stream"):
            print(f"\n[{reply_label}]")
            print(json.dumps(message, ensure_ascii=False, indent=2))
        elif message.get("tool_calls"):
            print("[完整工具请求]")
            print(json.dumps(message["tool_calls"], ensure_ascii=False, indent=2))
        finish_reason = response.finish_reason
        print(f"停止原因：{finish_reason}")
        if finish_reason == "stop":
            if request.get("stream"):
                print("\n[助手最终回答已接收完整]" if message.get("content") else "\n（本次没有返回文本回答）")
            else:
                print("\n[助手最终回答]")
                print(message.get("content") or "（本次没有返回文本回答）")
            if plan is not None and not plan.complete():
                print("任务暂停：计划尚未全部完成，或完成证据已过时；模型回答完整不代表计划已完成。")
                return "plan_incomplete"
            return "stop"
        if finish_reason != "tool_calls":
            if finish_reason == "length":
                print("本次任务未完成：模型输出达到 max_tokens 上限，回复被截断；未执行本轮工具，此前已完成的操作和结果保留。")
            else:
                print("本次任务未完成：模型请求异常结束，未执行本轮工具。")
            return "abnormal_finish"
        budget_reason = budget.pause_reason()
        if budget_reason:
            print("已达到总 token 停止阈值。" if budget_reason == "max_total_tokens" else "服务未提供有效的总 token 用量，无法继续核对预算。")
            print("任务暂停：未执行本轮工具，不再请求模型；此前已完成的操作保留。")
            return budget_reason
        if budget.model_calls >= budget.max_model_calls or step >= step_limit:
            reached_limit = budget.max_model_calls if budget.model_calls >= budget.max_model_calls else step_limit
            print(f"已达到最多 {reached_limit} 次模型请求，任务未完成；未执行本轮工具。")
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
            tool = registry.get(name)
            if tool is None:
                raise ValueError("未知工具，未执行。")
            arguments = json.loads(function["arguments"])
            tool.validate_arguments(arguments)
            prepared_calls.append((tool_call, name, tool, arguments))

        messages = [
            *request["messages"],
            {"role": "assistant", "content": message.get("content"), "tool_calls": calls},
        ]
        for index, (tool_call, name, tool, arguments) in enumerate(prepared_calls):
            try:
                decision = policy.decide(name, tool.read_only, tool.internal)
                prepared = None if decision == "deny" else tool.prepare(arguments)
                if decision == "ask":
                    record_event(log_file, "tool_waiting", step=step, tool=name, tool_call_id=tool_call["id"])
                approved, source = approve_tool(prepared.action if prepared else name, decision, "internal" if tool.internal else "auto_read")
                record_event(log_file, "tool_approval", step=step, tool=name, tool_call_id=tool_call["id"], approved=approved, source=source, decision=decision)
                if not approved:
                    print("已停止：未执行被拒绝的工具及本轮后续工具，也不会继续请求模型；此前已完成的操作保留。")
                    return "policy_denied" if decision == "deny" else "user_denied"

                record_event(log_file, "tool_started", step=step, tool=name, tool_call_id=tool_call["id"])
                result = {"ok": True, "tool": name, **prepared.execute()}
            except tools.TOOL_ERRORS as error:
                result = tools.tool_error(tool, arguments, error)
            if plan is not None:
                if name != "update_plan":
                    result["evidence_id"] = tool_call["id"]
                elif result.get("ok") is False:
                    result["available_evidence"] = plan.evidence_catalog()
            print("\n[工具执行结果]")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            record_event(log_file, "tool_result", step=step, tool=name, tool_call_id=tool_call["id"], result=result)
            if plan is not None:
                plan.observe(tool_call["id"], result)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": json.dumps(result, ensure_ascii=False)})
            halt_reason = result.get("halt_reason")
            if halt_reason in ("cancelled", "user_denied", "policy_denied", "max_model_calls", "max_total_tokens", "usage_unavailable"):
                if halt_reason == "max_model_calls":
                    for pending, pending_name, _, _ in prepared_calls[index + 1:]:
                        status = {"ok": False, "tool": pending_name, "source": "resume", "error": {
                            "type": "NotExecuted", "message": "子任务耗尽共享预算，本轮后续工具未执行；需要时重新请求。"}}
                        messages.append({"role": "tool", "tool_call_id": pending["id"], "content": json.dumps(status, ensure_ascii=False)})
                    record_event(log_file, "budget_paused", reason=halt_reason, full_messages=messages)
                print(f"子任务已要求停止（{halt_reason}），不执行后续工具或请求模型。")
                return halt_reason
            if result.get("cleanup_succeeded") is False:
                print("已停止：无法确认命令进程树已经清理；不执行后续工具或请求模型，请先核对现场。")
                return "error"
        request = {**request, "messages": messages}
