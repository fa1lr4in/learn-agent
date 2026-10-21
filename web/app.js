import { ApiClient, ApiError } from "./client.js";
import { PHASES, TOOL_STATES, historyItems, sessionPhase, taskItems } from "./view-model.js";

const ui = Object.fromEntries(["new-task", "new-chat", "sessions", "project-path", "project-title", "connection", "reconnect",
  "task-status", "timeline", "welcome", "messages", "approval", "approval-tool", "approval-command", "allow", "deny",
  "notice", "composer", "prompt", "stop", "send", "auto-read", "usage"].map(id => [id, document.getElementById(id)]));
const token = document.querySelector('meta[name="api-token"]');
const api = new ApiClient(token.content);
token.remove();
const workspace = document.querySelector('meta[name="workspace"]').content;
const storageKey = `learn-agent-current:${workspace}`;
const state = { session: null, task: null, history: [], sessions: [], events: [], cursor: null, connected: false, busy: false, deciding: false, error: "" };
const expandedTools = new Set();
let observer = null, rendering = false;

function element(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function Message(item) {
  const row = element("article", `message ${item.role}`), body = element("div", "message-body");
  body.append(element("p", "message-author", item.role === "user" ? "你" : "learn-agent"), element("p", "message-text", item.content));
  row.append(body);
  return row;
}

function ToolCard(item) {
  const card = element("article", "tool-card");
  card.dataset.tool = item.name;
  card.dataset.status = item.status;
  const heading = element("div", "tool-heading");
  heading.append(element("strong", "tool-title", item.action || `${item.name} · ${item.arguments.path || ""}`), element("span", `tool-status ${item.status}`, TOOL_STATES[item.status]));
  card.append(heading, element("p", "tool-name", `${item.name}${item.approvalSource ? ` · ${item.approvalSource === "user" ? (item.status === "denied" ? "已由你拒绝" : "已由你确认") : "按读取设置允许"}` : ""}`));
  const details = element("details", "source");
  details.open = expandedTools.has(item.id) || item.status === "waiting_approval";
  details.append(element("summary", "", "查看参数与结果"), element("pre", "", JSON.stringify(item.arguments, null, 2)));
  const diff = item.preview ?? item.result?.diff;
  if (diff !== undefined) details.append(element("p", "output-label", "代码差异"), element("pre", "diff", diff));
  if (item.result) {
    if (item.result.exit_code !== undefined) details.append(element("p", "output-label", `退出码 ${item.result.exit_code}`));
    for (const stream of ["stdout", "stderr"]) {
      if (stream in item.result) details.append(element("p", "output-label", stream), element("pre", "", item.result[stream] || "（空）"));
    }
    if (item.result.exit_code === undefined) details.append(element("pre", "", JSON.stringify(item.result, null, 2)));
  }
  details.addEventListener("toggle", () => {
    if (!details.isConnected) return;
    if (details.open) expandedTools.add(item.id); else expandedTools.delete(item.id);
  });
  card.append(details);
  return card;
}

function active() { return state.task && !state.task.finished; }
function phase() { return state.task?.state || sessionPhase(state.session?.reason); }
function continuable() { return !state.session?.reason || ["stop", "max_model_calls"].includes(state.session.reason); }
function remember() {
  try { localStorage.setItem(storageKey, JSON.stringify({ sessionId: state.session?.id, taskId: state.task?.id })); } catch { /* 本次会话仍可使用。 */ }
}
function scheduleRender() {
  if (!rendering) requestAnimationFrame(() => { rendering = false; render(); });
  rendering = true;
}

function render() {
  const follow = ui.timeline.scrollHeight - ui.timeline.scrollTop - ui.timeline.clientHeight < 120;
  const items = [...state.history, ...taskItems(state.events)];
  ui.messages.replaceChildren(...items.map(item => item.type === "message" ? Message(item) : ToolCard(item)));
  ui.welcome.hidden = items.length > 0;
  ui["task-status"].textContent = PHASES[phase()] || phase();
  ui["task-status"].dataset.state = phase();
  const blocked = active() || state.busy || !state.connected;
  ui.prompt.disabled = blocked || !continuable();
  ui.send.disabled = blocked || !continuable() || !ui.prompt.value.trim();
  ui["auto-read"].disabled = blocked;
  ui.stop.hidden = !active();
  ui.stop.disabled = state.busy || Boolean(state.task?.cancel_requested);
  ui["new-task"].disabled = blocked;
  ui["new-chat"].disabled = blocked;
  ui.reconnect.hidden = !state.error;
  ui.reconnect.disabled = state.busy;
  ui.connection.textContent = state.connected ? "已连接本地服务" : "连接已断开";
  const pending = state.task?.pending_approval;
  ui.approval.hidden = !pending || state.task?.cancel_requested;
  ui.allow.disabled = ui.deny.disabled = state.deciding || state.busy || !state.connected;
  if (pending) {
    ui["approval-tool"].textContent = pending.tool;
    ui["approval-command"].textContent = `${pending.action}\n\n${pending.preview ?? JSON.stringify(pending.arguments, null, 2)}`;
  }
  const notices = { idle: "修改文件和运行命令会逐项确认。", running: "任务进行中，可以停止后续操作。", waiting_approval: "请检查本次操作，允许后才会执行。",
    completed: "任务已完成，可以继续提问。", cancelled: "任务已取消，已有结果保留。", failed: "任务未正常完成，请查看已有结果。", paused: "任务已暂停，请查看原因和累计用量。" };
  ui.notice.textContent = state.error || (state.task?.cancel_requested && active() ? "正在停止，等待当前操作返回……" :
    (state.task?.state === "completed" && active() ? "正在保存会话……" :
      (!continuable() ? "此会话需要检查后恢复；可以新建会话继续。" : notices[phase()])));
  const summary = state.task?.summary;
  ui.usage.textContent = summary ? `${summary.model_calls} 次请求 · ${summary.usage.total_tokens ?? "未知"} token · ${state.task.reason || "收尾中"}${state.task.finished ? (state.task.session_saved ? " · 会话已保存" : " · 会话保存失败") : ""}` : "Enter 发送 · Shift + Enter 换行";
  ui.sessions.replaceChildren(...state.sessions.map(session => {
    const result = { stop: "已完成", user_denied: "已拒绝", policy_denied: "已拒绝", cancelled: "已取消", max_model_calls: "已暂停", max_total_tokens: "已暂停" }[session.reason] || (session.reason ? "已结束" : "新会话");
    const button = element("button", `session${state.session?.id === session.id ? " selected" : ""}`, `${new Date(session.updated_at || session.created_at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })} · ${result}`);
    button.type = "button"; button.title = session.id; button.disabled = Boolean(blocked);
    button.addEventListener("click", () => selectSession(session.id));
    return button;
  }));
  if (follow) ui.timeline.scrollTop = ui.timeline.scrollHeight;
}

async function refreshSessions() { state.sessions = await api.request("/sessions"); scheduleRender(); }
function snapshot(task) {
  state.task = task; state.events = task.events; state.cursor = task.last_event_id;
  if (task.finished && state.session) state.session.reason = task.reason;
  remember(); scheduleRender();
}
function accept(frame) {
  if (frame.event === "snapshot") snapshot(frame.data);
  else {
    if (frame.event === "task_finished") {
      state.task = { ...state.task, ...frame.data };
      state.session.reason = frame.data.reason;
    } else if (frame.event === "approval_required") state.task.pending_approval = frame.data;
    else if (frame.event === "approval_submitted") state.task.pending_approval = null;
    else if (frame.event === "cancel_requested") { state.task.cancel_requested = true; state.task.pending_approval = null; }
    else {
      state.events.push(frame.data);
      if (frame.data.task_state) state.task.state = frame.data.task_state;
      if (frame.event === "run_end") { state.task.reason = frame.data.reason; state.task.summary = frame.data.summary; }
    }
    state.cursor = frame.id; state.task.last_event_id = frame.id;
    remember(); scheduleRender();
  }
}

async function observe() {
  observer?.abort();
  const control = new AbortController();
  observer = control;
  let retries = 0;
  while (active() && !control.signal.aborted) {
    try {
      await api.watch(state.task.id, state.cursor, frame => { accept(frame); retries = 0; state.error = ""; }, control.signal);
      if (control.signal.aborted) return;
      if (active()) snapshot(await api.request(`/tasks/${state.task.id}`));
      if (active()) throw new Error("事件连接已断开。");
    } catch (error) {
      if (control.signal.aborted) return;
      if (++retries > 3 || error.status) {
        state.error = `${error.message} 请重新连接；原任务不会重新提交。`;
        state.connected = false; render(); return;
      }
      state.error = "正在恢复事件连接……"; render();
      await new Promise(resolve => setTimeout(resolve, 600));
    }
  }
  if (!control.signal.aborted) {
    state.error = state.task?.persistence_error ? "任务结果已保留，但会话保存失败，请查看日志。" : "";
    await refreshSessions().catch(error => { state.error = error.message; });
    render();
  }
}

async function openTask(task) {
  state.session = await api.request(`/sessions/${task.session_id}`);
  const initial = task.events.find(event => event.event === "run_start")?.initial_messages;
  state.history = historyItems(initial ? initial.slice(1, -1) : state.session.messages, state.session.tool_runs);
  snapshot(task);
  if (!task.finished) void observe();
}

async function connect() {
  state.busy = true; state.error = ""; render();
  try {
    await refreshSessions();
    const tasks = await api.request("/tasks");
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(storageKey) || "{}"); } catch { /* 无历史选择。 */ }
    const running = tasks.findLast(task => !task.finished);
    const chosen = running || tasks.findLast(task => task.session_id === saved.sessionId);
    if (chosen) await openTask(await api.request(`/tasks/${chosen.id}`));
    else if (saved.sessionId && state.sessions.some(session => session.id === saved.sessionId)) await loadSession(saved.sessionId);
    state.connected = true;
  } catch (error) { state.error = error.message; state.connected = false; }
  finally { state.busy = false; render(); }
}

async function loadSession(id) {
  observer?.abort();
  state.session = await api.request(`/sessions/${id}`);
  state.history = historyItems(state.session.messages, state.session.tool_runs);
  state.task = null; state.events = []; state.cursor = null;
  expandedTools.clear(); remember();
}
async function selectSession(id) {
  if (active() || state.busy) return;
  state.busy = true;
  try { await loadSession(id); state.error = ""; } catch (error) { state.error = error.message; }
  finally { state.busy = false; render(); }
}
function newSession() {
  if (active() || state.busy) return;
  observer?.abort();
  Object.assign(state, { session: null, task: null, events: [], history: [], cursor: null, error: "" });
  expandedTools.clear(); ui.prompt.value = "";
  remember(); render(); ui.prompt.focus();
}

async function send(event) {
  event.preventDefault();
  if (active() || state.busy || !state.connected || !continuable() || !ui.prompt.value.trim()) return;
  const prompt = ui.prompt.value.trim();
  let knownTasks = new Set(), submitted = false;
  state.busy = true; state.error = ""; render();
  try {
    if (!state.session) { state.session = await api.request("/sessions", {}); remember(); }
    state.session = await api.request(`/sessions/${state.session.id}`);
    state.history = historyItems(state.session.messages, state.session.tool_runs);
    knownTasks = new Set((await api.request("/tasks")).map(task => task.id));
    submitted = true;
    const task = await api.request(`/sessions/${state.session.id}/tasks`, { prompt, stream: true, auto_read: ui["auto-read"].checked, max_model_calls: 8 });
    snapshot(task); ui.prompt.value = ""; void observe();
  } catch (error) {
    if (error instanceof ApiError || !submitted) { state.error = error.message; return; }
    // 提交响应丢失时先查找已接收的任务，绝不自动重发 POST。
    try {
      const tasks = await api.request("/tasks");
      const received = tasks.findLast(task => task.session_id === state.session?.id && !knownTasks.has(task.id));
      if (received) await openTask(await api.request(`/tasks/${received.id}`));
      else { state.error = "提交结果尚未确认，请重新连接检查任务。"; state.connected = false; }
    } catch { state.error = "提交结果尚未确认，请重新连接检查任务。"; state.connected = false; }
  } finally { state.busy = false; render(); }
}

async function decide(approved) {
  const pending = state.task?.pending_approval;
  if (!pending || state.deciding || state.busy) return;
  state.deciding = true; state.error = ""; render();
  try {
    await api.request(`/tasks/${state.task.id}/approvals`, { approval_id: pending.id, approved });
    if (state.task.pending_approval?.id === pending.id) state.task.pending_approval = null;
  } catch (error) {
    state.error = error.message;
    try { snapshot(await api.request(`/tasks/${state.task.id}`)); } catch { state.connected = false; }
  } finally { state.deciding = false; render(); }
}
async function stop() {
  if (!active() || state.busy || state.task.cancel_requested) return;
  state.busy = true; state.error = ""; render();
  try { await api.request(`/tasks/${state.task.id}/cancel`, {}); state.task.cancel_requested = true; state.task.pending_approval = null; }
  catch (error) { state.error = error.message; }
  finally { state.busy = false; render(); }
}

ui["project-path"].textContent = workspace;
ui["project-title"].textContent = workspace.split(/[\\/]/).at(-1) || "工作台";
ui.composer.addEventListener("submit", send);
ui.prompt.addEventListener("input", () => { ui.send.disabled = Boolean(active() || state.busy || !state.connected || !continuable() || !ui.prompt.value.trim()); });
ui.prompt.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); ui.composer.requestSubmit(); }
});
ui.allow.addEventListener("click", () => decide(true));
ui.deny.addEventListener("click", () => decide(false));
ui.stop.addEventListener("click", stop);
ui["new-task"].addEventListener("click", newSession);
ui["new-chat"].addEventListener("click", newSession);
ui.reconnect.addEventListener("click", connect);
void connect();
