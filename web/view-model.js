export const PHASES = { idle: "就绪", running: "进行中", waiting_approval: "等待确认", completed: "已完成", cancelled: "已取消", failed: "失败", paused: "已暂停" };
export const TOOL_STATES = { queued: "尚未执行", waiting_approval: "等待确认", running: "进行中", completed: "完成", failed: "失败", denied: "已拒绝", cancelled: "未执行", unknown: "结果未确认" };

function argumentsOf(call) {
  const raw = call.function?.arguments;
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object" && !Array.isArray(value) ? value : { raw };
  } catch { return { raw }; }
}

export function historyItems(messages = []) {
  const items = [], calls = new Map();
  for (const [index, message] of messages.entries()) {
    if (["user", "assistant"].includes(message.role) && message.content) items.push({ type: "message", role: message.role, content: message.content });
    for (const call of message.tool_calls || []) {
      const item = { type: "tool", id: `history-${index}-${call.id}`, name: call.function?.name || "无效工具请求", arguments: argumentsOf(call), status: "queued" };
      calls.set(call.id, item);
      items.push(item);
    }
    if (message.role === "tool" && calls.has(message.tool_call_id)) {
      const item = calls.get(message.tool_call_id);
      try { item.result = JSON.parse(message.content); } catch { item.result = { content: message.content }; }
      item.status = item.result.error?.type === "NotExecuted" ? "cancelled" : item.result.ok === false ? "failed" : "completed";
    }
  }
  return items;
}

// 模型提出工具请求时只创建卡片，不表示已经执行。
export function taskItems(events = []) {
  const items = [], messages = new Map(), tools = new Map();
  function assistant(step) {
    if (!messages.has(step)) {
      const item = { type: "message", role: "assistant", content: "" };
      messages.set(step, item); items.push(item);
    }
    return messages.get(step);
  }
  function tool(event, name, args) {
    const id = `${event.step}-${event.tool_call_id}`;
    if (!tools.has(id)) {
      const item = { type: "tool", id, name, arguments: args || {}, status: "queued" };
      tools.set(id, item); items.push(item);
    }
    return tools.get(id);
  }
  for (const event of events) {
    if (event.event === "run_start") {
      const user = event.initial_messages?.findLast(m => m.role === "user");
      if (user) items.push({ type: "message", role: "user", content: user.content });
    } else if (event.event === "model_delta" && event.delta.content) assistant(event.step).content += event.delta.content;
    else if (event.event === "model_response") {
      const message = event.response.choices[0].message;
      if (message.content || messages.has(event.step)) assistant(event.step).content = message.content || "";
      for (const call of message.tool_calls || []) tool({ ...event, tool_call_id: call.id }, call.function?.name || "无效工具请求", argumentsOf(call));
    } else if (event.event.startsWith("tool_")) {
      const item = tool(event, event.tool, event.arguments);
      if (event.arguments) item.arguments = event.arguments;
      if (event.action) item.action = event.action;
      if (event.preview !== undefined) item.preview = event.preview;
      if (event.event === "tool_waiting") item.status = "waiting_approval";
      if (event.event === "tool_approval") { item.status = event.approved ? "queued" : "denied"; item.approvalSource = event.source; }
      if (event.event === "tool_started") item.status = "running";
      if (event.event === "tool_result") { item.result = event.result; item.status = event.result.ok === false ? "failed" : "completed"; }
    } else if (event.event === "run_end") {
      for (const item of tools.values()) {
        if (item.status === "running") item.status = "unknown";
        else if (["queued", "waiting_approval"].includes(item.status)) item.status = "cancelled";
      }
    }
  }
  return items.filter(item => item.type !== "message" || item.content);
}
