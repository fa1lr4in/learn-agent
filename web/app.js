import { DEMO } from "./demo-data.js";

const ui = Object.fromEntries([
  "new-task", "reset-task", "task-status", "timeline", "welcome", "messages", "approval",
  "approval-tool", "approval-command", "allow", "deny", "notice", "composer",
  "prompt", "stop", "send", "progress-read", "progress-tests", "progress-entry",
].map(id => [id, document.getElementById(id)]));

const PHASES = { idle: "就绪", running: "进行中", waiting_approval: "等待确认", completed: "已完成", cancelled: "已取消" };
const TOOL_STATES = { running: "进行中", waiting_approval: "等待确认", completed: "完成", denied: "已拒绝", cancelled: "已取消" };
const state = { phase: "idle", items: [], executionIndex: 0 };
const expandedTools = new Set();
let timer = null;

function element(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// 组件就是根据一份数据，创建一块界面的函数。
function Message(item) {
  const row = element("article", `message ${item.role}`);
  const avatar = element("span", "avatar", item.role === "user" ? "你" : "a");
  avatar.setAttribute("aria-hidden", "true");
  const body = element("div", "message-body");
  body.append(element("p", "message-author", item.role === "user" ? "你" : "learn-agent"), element("p", "message-text", item.content));
  row.append(avatar, body);
  return row;
}

function ToolCard(item) {
  const card = element("article", "tool-card");
  card.dataset.tool = item.name;
  card.dataset.status = item.status;
  const heading = element("div", "tool-heading");
  const title = element("div", "tool-title");
  title.append(element("span", "tool-symbol", "⌘"), element("strong", "", item.title));
  heading.append(title, element("span", `tool-status ${item.status}`, TOOL_STATES[item.status]));
  card.append(heading, element("p", "tool-name", `${item.name} · 示例工具`));
  if (item.command) card.append(element("code", "command", item.command));
  if (item.content !== undefined && item.status === "completed") {
    const details = element("details", "source");
    details.open = expandedTools.has(item.id);
    details.append(element("summary", "", "查看示例源码"), element("pre", "", item.content));
    details.addEventListener("toggle", () => {
      if (!details.isConnected) return;
      if (details.open) expandedTools.add(item.id);
      else expandedTools.delete(item.id);
    });
    card.append(details);
  }
  if (item.result) {
    const output = element("div", "tool-output");
    output.append(element("p", "output-label", `退出码 ${item.result.exit_code} · 示例结果`));
    for (const stream of ["stdout", "stderr"]) {
      output.append(element("p", "output-label", stream), element("pre", "", item.result[stream] || "（空）"));
    }
    card.append(output);
  }
  return card;
}

function ApprovalPanel() {
  const waiting = state.phase === "waiting_approval";
  ui.approval.hidden = !waiting;
  if (waiting) {
    const step = DEMO.executions[state.executionIndex];
    ui["approval-tool"].textContent = `${step.name} · ${step.title}`;
    ui["approval-command"].textContent = step.command;
  }
}

function render() {
  const follow = ui.timeline.scrollHeight - ui.timeline.scrollTop - ui.timeline.clientHeight < 120;
  ui.messages.replaceChildren(...state.items.map(item => item.type === "message" ? Message(item) : ToolCard(item)));
  ui.welcome.hidden = state.items.length > 0;
  ui["task-status"].textContent = PHASES[state.phase];
  ui["task-status"].dataset.state = state.phase;
  ui.prompt.disabled = state.phase !== "idle";
  ui.send.disabled = state.phase !== "idle" || !ui.prompt.value.trim();
  ui.stop.hidden = !["running", "waiting_approval"].includes(state.phase);
  const notices = {
    idle: "输入会显示为用户消息，后续按固定示例流程演示。",
    running: "正在展示示例工具过程，可以随时停止。",
    waiting_approval: "请检查上方操作；每次运行分别确认。",
    completed: "本次示例已完成。点击「新建示例」可以重新开始。",
    cancelled: "本次示例已取消，已有结果保留。点击「新建示例」可以重试。",
  };
  ui.notice.textContent = notices[state.phase];
  const reads = state.items.filter(item => item.name === "read_file");
  const stages = {
    read: reads.length ? (reads.every(item => item.status === "completed") ? "completed" : reads[0].status) : "pending",
    tests: state.items.find(item => item.id === "tests")?.status || "pending",
    entry: state.items.find(item => item.id === "entry")?.status || "pending",
  };
  for (const [stage, status] of Object.entries(stages)) ui[`progress-${stage}`].dataset.state = status;
  ApprovalPanel();
  if (follow) ui.timeline.scrollTop = ui.timeline.scrollHeight;
}

function schedule(callback) {
  clearTimeout(timer);
  // 只模拟等待时间，不发请求，也不执行工具。
  timer = setTimeout(() => {
    timer = null;
    callback();
    render();
  }, 800);
}

function requestApproval() {
  const step = DEMO.executions[state.executionIndex];
  state.items.push({ type: "tool", ...step, result: null, status: "waiting_approval" });
  state.phase = "waiting_approval";
}

function startDemo(event) {
  event.preventDefault();
  if (state.phase !== "idle" || !ui.prompt.value.trim()) return;
  state.phase = "running";
  state.items = [
    { type: "message", role: "user", content: ui.prompt.value.trim() },
    { type: "message", role: "assistant", content: "先核对两份源码，再分别请求运行验收和入口。下面展示的是固定示例流程。" },
    ...DEMO.files.map(file => ({ type: "tool", id: file.path, name: "read_file", title: `读取 ${file.path}`, content: file.content, status: "running" })),
  ];
  render();
  schedule(() => {
    for (const item of state.items.filter(item => item.type === "tool")) item.status = "completed";
    requestApproval();
  });
}

function approveTool() {
  if (state.phase !== "waiting_approval") return;
  const step = DEMO.executions[state.executionIndex];
  const tool = state.items.find(item => item.id === step.id);
  tool.status = "running";
  state.phase = "running";
  render();
  schedule(() => {
    tool.status = "completed";
    tool.result = step.result;
    state.executionIndex += 1;
    if (state.executionIndex < DEMO.executions.length) requestApproval();
    else {
      state.phase = "completed";
      state.items.push({ type: "message", role: "assistant", content: DEMO.conclusion });
    }
  });
}

function cancelDemo(denied = false) {
  if (!["running", "waiting_approval"].includes(state.phase)) return;
  clearTimeout(timer);
  timer = null;
  for (const item of state.items) {
    if (item.type === "tool" && ["running", "waiting_approval"].includes(item.status)) item.status = denied ? "denied" : "cancelled";
  }
  state.phase = "cancelled";
  state.items.push({ type: "message", role: "assistant", content: denied ? "你拒绝了这次操作，示例在此停止。此前已经展示的结果保留。" : "示例已停止，不再展示后续结果。此前已经展示的结果保留。" });
  render();
}

function resetDemo() {
  clearTimeout(timer);
  timer = null;
  Object.assign(state, { phase: "idle", items: [], executionIndex: 0 });
  expandedTools.clear();
  ui.prompt.value = DEMO.prompt;
  render();
  ui.prompt.focus();
}

ui.composer.addEventListener("submit", startDemo);
ui.prompt.addEventListener("input", () => { ui.send.disabled = !ui.prompt.value.trim(); });
ui.prompt.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    ui.composer.requestSubmit();
  }
});
ui.allow.addEventListener("click", approveTool);
ui.deny.addEventListener("click", () => cancelDemo(true));
ui.stop.addEventListener("click", () => cancelDemo());
ui["new-task"].addEventListener("click", resetDemo);
ui["reset-task"].addEventListener("click", resetDemo);
resetDemo();
