const fs = require("node:fs");
const path = require("node:path");
const { Backend, sameDirectory } = require("./backend.cjs");

function authorizeFrame(event, win, info) {
  if (event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame ||
      event.senderFrame.url !== `${info.url}/`) {
    throw new Error("不允许的项目选择请求。");
  }
}

async function chooseProject(event, win, info, dialog, args = [], switchProject) {
  authorizeFrame(event, win, info);
  if (args.length) throw new Error("项目选择不接受页面提供的路径。");
  const selected = await dialog.showOpenDialog(win, {
    title: "选择项目目录", defaultPath: info.workspace, properties: ["openDirectory"]
  });
  if (selected.canceled || !selected.filePaths.length) return null;
  const chosen = fs.realpathSync(selected.filePaths[0]);
  if (!fs.statSync(chosen).isDirectory()) throw new Error("请选择已有目录。");
  const current = sameDirectory(chosen, info.workspace);
  if (!current) await switchProject(chosen);
  return { path: chosen, current };
}

function createWindow(info, { BrowserWindow, ipcMain, dialog }, switchProject, themes) {
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
  let choosing = false;
  ipcMain.handle("project:choose", async (event, ...args) => {
    if (choosing) throw new Error("项目选择正在进行中。");
    choosing = true;
    try { return await chooseProject(event, win, info, dialog, args, switchProject); }
    finally { choosing = false; }
  });
  ipcMain.on("theme:get", event => {
    try { authorizeFrame(event, win, info); event.returnValue = themes.value; }
    catch { event.returnValue = null; }
  });
  ipcMain.handle("theme:set", (event, value) => {
    authorizeFrame(event, win, info);
    if (!["system", "light", "dark"].includes(value)) throw new Error("无效主题。");
    themes.save(value);
  });
  win.on("closed", () => {
    ipcMain.removeHandler("project:choose"); ipcMain.removeHandler("theme:set"); ipcMain.removeAllListeners("theme:get");
  });
  win.once("ready-to-show", () => win.show());
  return { win, loaded: win.loadURL(`${info.url}/`) };
}

function startDesktop(electron) {
  const { app, dialog } = electron;
  if (!app.requestSingleInstanceLock()) { app.quit(); return; }
  let backend, win, info, quitting = false, canQuit = false, switching = Promise.resolve();
  const root = path.join(__dirname, "..");
  const fail = error => {
    if (!quitting) dialog.showErrorBox("learn-agent 本地服务异常", error.message);
    app.quit();
  };
  const newBackend = () => new Backend({ root, onUnexpectedExit: () => fail(new Error("Python 后端意外退出。请查看 logs/.desktop-error.log，再重新打开应用；任务不会自动重放。")) });
  app.on("second-instance", () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", event => {
    if (canQuit) return;
    event.preventDefault();
    if (quitting) return;
    quitting = true;
    if (win && !win.isDestroyed()) { win.setTitle("learn-agent · 正在取消任务并保存会话……"); win.setEnabled(false); }
    void (async () => {
      try {
        await startup.catch(() => {});
        await switching.catch(() => {});
        const result = await backend?.stop();
        if (result?.forced) dialog.showErrorBox("后端已强制结束", "当前操作未在 15 秒内返回，已清理本应用启动的进程树。当前会话可能未保存完整，请检查日志。再次打开不会自动重放任务。");
      } catch (error) { dialog.showErrorBox("关闭后端失败", error.message); }
      finally { canQuit = true; app.quit(); }
    })();
  });
  const startup = app.whenReady().then(async () => {
    const workspaceArg = process.argv.indexOf("--workspace");
    const workspace = workspaceArg < 0 ? path.join(root, "workspace", "demo_project") : process.argv[workspaceArg + 1];
    if (!workspace) throw new Error("--workspace 后需要提供项目目录。");
    backend = newBackend();
    info = await backend.start(path.resolve(workspace));
    if (quitting) return;
    const themeFile = path.join(app.getPath("userData"), "theme.json");
    const themes = { value: "system", save(value) {
      fs.writeFileSync(themeFile, JSON.stringify({ theme: value })); this.value = value;
    } };
    try {
      const saved = JSON.parse(fs.readFileSync(themeFile, "utf8")).theme;
      if (["system", "light", "dark"].includes(saved)) themes.value = saved;
    } catch { /* first launch has no stored desktop theme */ }
    const switchProject = project => {
      let stopped = false;
      switching = (async () => {
        if (quitting) throw new Error("应用正在关闭。");
        const response = await fetch(`${info.url}/tasks`, { headers: { Authorization: `Bearer ${info.token}` },
          redirect: "error", signal: AbortSignal.timeout(3000) });
        if (!response.ok) throw new Error("无法检查当前任务，请先重新连接。");
        if ((await response.json()).some(task => !task.finished)) throw new Error("当前任务尚未保存完成，请先完成或停止任务，再切换项目。");
        const result = await backend.stop();
        stopped = true;
        if (result.forced) throw new Error("旧后端未正常收尾，请查看诊断日志后重新打开应用。");
        if (quitting) return;
        backend = newBackend();
        Object.assign(info, await backend.start(project));
        if (!quitting) await win.loadURL(`${info.url}/`);
      })();
      // Once the old server stops, a failed switch must not leave a window on its stale origin.
      switching.catch(error => { if (stopped || backend.exited) fail(error); });
      return switching;
    };
    const created = createWindow(info, electron, switchProject, themes);
    win = created.win;
    win.on("close", event => { if (!canQuit) { event.preventDefault(); app.quit(); } });
    const { loaded } = created;
    await loaded;
  });
  startup.catch(fail);
}

if (process.versions.electron) startDesktop(require("electron"));
module.exports = { chooseProject, createWindow, startDesktop };
