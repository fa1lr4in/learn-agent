const fs = require("node:fs");
const path = require("node:path");

function readBackendInfo(file = path.join(__dirname, "..", "logs", ".api-server.json")) {
  const info = JSON.parse(fs.readFileSync(file, "utf8"));
  const url = new URL(info.url);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.origin !== info.url || typeof info.token !== "string" ||
      !/^[A-Za-z0-9_-]{20,}$/.test(info.token) || typeof info.workspace !== "string" || !path.isAbsolute(info.workspace)) {
    throw new Error("本地服务信息无效，请重新启动 Python 后端。");
  }
  const workspace = fs.realpathSync(info.workspace);
  if (!fs.statSync(workspace).isDirectory()) throw new Error("本地服务的项目目录不存在。");
  return { url: info.url, token: info.token, workspace };
}

async function checkBackend(info, fetcher = fetch) {
  const response = await fetcher(`${info.url}/tasks`, {
    headers: { Authorization: `Bearer ${info.token}` },
    redirect: "error", signal: AbortSignal.timeout(3000)
  });
  if (!response.ok || !Array.isArray(await response.json())) throw new Error("本地服务未就绪，请重新启动 Python 后端。");
}

function sameDirectory(first, second) {
  return process.platform === "win32" ? first.toLowerCase() === second.toLowerCase() : first === second;
}

async function chooseProject(event, win, info, dialog, args = []) {
  if (event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame ||
      event.senderFrame.url !== `${info.url}/` || args.length) {
    throw new Error("不允许的项目选择请求。");
  }
  const selected = await dialog.showOpenDialog(win, {
    title: "选择项目目录", defaultPath: info.workspace, properties: ["openDirectory"]
  });
  if (selected.canceled || !selected.filePaths.length) return null;
  const chosen = fs.realpathSync(selected.filePaths[0]);
  if (!fs.statSync(chosen).isDirectory()) throw new Error("请选择已有目录。");
  return { path: chosen, current: sameDirectory(chosen, info.workspace) };
}

function createWindow(info, { BrowserWindow, ipcMain, dialog }) {
  const win = new BrowserWindow({
    title: "learn-agent", width: 1200, height: 820, minWidth: 900, minHeight: 620,
    show: false, backgroundColor: "#1c1c1c",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true, webviewTag: false
    }
  });
  win.setMenu(null);
  const contents = win.webContents;
  const restrictNavigation = (event, url) => { if (url !== `${info.url}/`) event.preventDefault(); };
  contents.on("will-navigate", restrictNavigation);
  contents.on("will-redirect", restrictNavigation);
  contents.setWindowOpenHandler(() => ({ action: "deny" }));
  contents.session.setPermissionRequestHandler((_contents, _permission, answer) => answer(false));
  contents.session.setPermissionCheckHandler(() => false);
  ipcMain.handle("project:choose", (event, ...args) => chooseProject(event, win, info, dialog, args));
  win.on("closed", () => ipcMain.removeHandler("project:choose"));
  win.once("ready-to-show", () => win.show());
  return { win, loaded: win.loadURL(`${info.url}/`) };
}

function startDesktop(electron) {
  const { app, dialog } = electron;
  app.on("window-all-closed", () => app.quit());
  app.whenReady().then(async () => {
    const info = readBackendInfo();
    await checkBackend(info);
    const { loaded } = createWindow(info, electron);
    await loaded;
  }).catch(() => {
    dialog.showErrorBox("无法打开 learn-agent", "请先在终端启动本项目的 Python 后端，再重新打开桌面窗口。启动方法见 README 的桌面窗口部分。");
    app.quit();
  });
}

if (process.versions.electron) startDesktop(require("electron"));
module.exports = { readBackendInfo, checkBackend, chooseProject, createWindow };
