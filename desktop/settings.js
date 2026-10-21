const api = window.learnAgentSettings;
const ui = Object.fromEntries(["title", "settings-form", "workspace", "choose", "model", "api-key", "key-status", "message",
  "cancel", "save", "logs", "diagnostics", "diagnostic-output"].map(id => [id, document.getElementById(id)]));
let busy = false;

async function action(work) {
  if (busy) return;
  busy = true;
  for (const node of document.querySelectorAll("button, input")) node.disabled = true;
  try { await work(); } catch (error) { ui.message.textContent = error.message; }
  finally { busy = false; for (const node of document.querySelectorAll("button, input")) node.disabled = false; }
}

ui["settings-form"].addEventListener("submit", event => {
  event.preventDefault();
  void action(async () => {
    const input = { workspace: ui.workspace.value, model: ui.model.value, apiKey: ui["api-key"].value };
    ui["api-key"].value = "";
    ui.message.textContent = "正在保存设置并启动本地服务……";
    await api.save(input);
  });
});
ui.choose.addEventListener("click", () => void action(async () => {
  const chosen = await api.chooseProject(); if (chosen) ui.workspace.value = chosen;
}));
ui.cancel.addEventListener("click", () => void action(() => api.cancel()));
ui.logs.addEventListener("click", () => void action(() => api.openLogs()));
ui.diagnostics.addEventListener("click", () => void action(async () => {
  ui["diagnostic-output"].textContent = JSON.stringify(await api.diagnostics(), null, 2);
  ui["diagnostic-output"].hidden = false;
}));

void action(async () => {
  const settings = await api.read();
  ui.workspace.value = settings.workspace || "";
  ui.model.value = settings.model;
  ui["key-status"].textContent = { saved: "已保存本机加密密钥，留空即可继续使用。", environment: "已读取环境变量中的密钥，留空可直接保存，无需重新输入。",
    missing: "尚未配置密钥，请只在本窗口输入。" }[settings.keySource];
  ui.title.textContent = settings.initial ? "开始使用 learn-agent" : "设置";
  ui.save.textContent = settings.initial ? "保存并启动" : "保存并重启后端";
  ui.message.textContent = settings.warning || (settings.initial ? "" : "保存后会重启本地服务，已有历史保留。");
  document.documentElement.dataset.theme = settings.theme === "system" ?
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : settings.theme;
});
