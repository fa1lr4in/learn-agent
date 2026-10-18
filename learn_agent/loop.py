"""Bounded model/tool loop, independent of individual tool names."""

import json
from typing import TextIO

from . import tools
from .events import record_event
from .model import call_model
from .permissions import approve_tool


MAX_MODEL_CALLS = 8


def run_agent(payload: dict, api_key: str, log_file: TextIO | None = None, auto_read: bool = False) -> str:
    request = {**payload, "tools": tools.tool_schemas(), "tool_choice": "auto"}
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
            if finish_reason == "length":
                print("本次任务未完成：模型输出达到 max_tokens 上限，回复被截断；未执行本轮工具，此前已完成的操作和结果保留。")
            else:
                print("本次任务未完成：模型请求异常结束，未执行本轮工具。")
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
            tool = tools.TOOL_REGISTRY.get(name)
            if tool is None:
                raise ValueError("未知工具，未执行。")
            arguments = json.loads(function["arguments"])
            tool.validate_arguments(arguments)
            prepared_calls.append((tool_call, name, tool, arguments))

        messages = [
            *request["messages"],
            {"role": "assistant", "content": message.get("content"), "tool_calls": calls},
        ]
        for tool_call, name, tool, arguments in prepared_calls:
            try:
                prepared = tool.prepare(arguments)
                approved, source = approve_tool(prepared.action, tool.read_only, auto_read)
                record_event(log_file, "tool_approval", step=step, tool=name, tool_call_id=tool_call["id"], approved=approved, source=source)
                if not approved:
                    print("已停止：未执行被拒绝的工具及本轮后续工具，也不会继续请求模型；此前已完成的操作保留。")
                    return "user_denied"

                result = {"ok": True, "tool": name, **prepared.execute()}
            except tools.TOOL_ERRORS as error:
                result = tools.tool_error(tool, arguments, error)
            print("\n[工具执行结果]")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            record_event(log_file, "tool_result", step=step, tool=name, tool_call_id=tool_call["id"], result=result)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": json.dumps(result, ensure_ascii=False)})
        request = {**request, "messages": messages}
