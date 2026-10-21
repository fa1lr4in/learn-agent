const fs = require("node:fs");
const path = require("node:path");
const { Backend, sameDirectory } = require("./backend.cjs");
const { Settings } = require("./settings.cjs");
const { createSettingsWindow } = require("./settings-window.cjs");
const { runtimePaths, prepareDemo } = require("./runtime.cjs");

function authorizeFrame(event, win, info) {
  if (event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame ||
      event.senderFrame.url !== `${info.url}/`) {
    throw new Error("不允许的桌面请求。");
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

function createWindow(info, { BrowserWindow, ipcMain, dialog }, switchProject, themes, openSettings) {
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
  ipcMain.handle("desktop:settings", (event, ...args) => {
    authorizeFrame(event, win, info);
    if (args.length) throw new Error("打开设置不接受参数。");
    return openSettings();
  });
  win.on("closed", () => {
    for (const name of ["project:choose", "theme:set", "desktop:settings"]) ipcMain.removeHandler(name);
    ipcMain.removeAllListeners("theme:get");
  });
  win.once("ready-to-show", () => win.show());
  return { win, loaded: win.loadURL(`${info.url}/`) };
}

function startDesktop(electron) {
  const { app, dialog } = electron;
  if (!app.requestSingleInstanceLock()) { app.quit(); return; }
  let backend, win, settingsWindow, settings, themes;
  let quitting = false, canQuit = false, changing = false, switching = Promise.resolve();
  const info = {};
  const paths = runtimePaths(app);
  const fail = error => {
    if (!quitting) dialog.showErrorBox("learn-agent 本地服务异常", error.message);
    app.quit();
  };
  const newBackend = () => new Backend({ ...paths, env: settings.environment(),
    onUnexpectedExit: () => fail(new Error("Python 后端意外退出。请查看 logs/.desktop-error.log，再重新打开应用；任务不会自动重放。")) });
  const assertIdle = async () => {
    if (!backend?.info || backend.exited) return;
    const response = await fetch(`${info.url}/tasks`, { headers: { Authorization: `Bearer ${info.token}` },
      redirect: "error", signal: AbortSignal.timeout(3000) });
    if (!response.ok) throw new Error("无法检查当前任务，请先重新连接。");
    if ((await response.json()).some(task => !task.finished)) throw new Error("当前任务尚未保存完成，请先完成或停止任务，再更换配置或项目。");
  };
  const change = operation => {
    if (quitting || changing) return Promise.reject(new Error("应用正在切换或关闭，请稍候。"));
    changing = true;
    switching = (async () => { try { await assertIdle(); await operation(); } finally { changing = false; } })();
    return switching;
  };
  const restartBackend = async project => {
    const result = await backend?.stop();
    if (result?.forced) throw new Error("旧后端未正常收尾，请查看诊断日志后重新打开应用。");
    if (quitting) return;
    backend = newBackend();
    Object.assign(info, await backend.start(project));
    if (quitting) return;
    if (win) await win.loadURL(`${info.url}/`);
    else {
      const created = createWindow(info, electron, switchProject, themes, async () => { await assertIdle(); showSettings(); });
      win = created.win;
      win.on("close", event => { if (!canQuit) { event.preventDefault(); app.quit(); } });
      await created.loaded;
    }
  };
  const showSettings = (notice = "") => {
    if (settingsWindow) { settingsWindow.focus(); return; }
    const created = createSettingsWindow(electron, {
      parent: win, settings, theme: themes.value, notice, logDirectory: paths.logDirectory,
      save: input => change(async () => { await settings.save(input); await restartBackend(settings.data.workspace); }),
      diagnostics: () => {
        let latestError = "";
        try { latestError = fs.readFileSync(path.join(paths.logDirectory, ".desktop-error.log"), "utf8").slice(-6000); }
        catch (error) { if (error.code !== "ENOENT") latestError = "无法读取启动诊断文件。"; }
        return { electron: process.versions.electron, node: process.versions.node, ...settings.view(),
          packaged: Boolean(app.isPackaged), version: app.getVersion?.(),
          backendRunning: Boolean(backend?.info && !backend.exited),
          pythonExists: fs.existsSync(paths.python),
          settingsFile: settings.file, logDirectory: paths.logDirectory, latestError: settings.redact(latestError) };
      },
      closed: () => { settingsWindow = null; if (!win || !backend?.info || backend.exited) app.quit(); }
    });
    settingsWindow = created.win;
    created.loaded.catch(fail);
  };
  const switchProject = project => change(async () => {
    settings.rememberProject(project);
    try { await restartBackend(project); }
    catch (error) { showSettings(error.message); throw error; }
  });
  app.on("second-instance", () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", event => {
    if (canQuit) return;
    event.preventDefault();
    if (quitting) return;
    quitting = true;
    if (win && !win.isDestroyed()) { win.setTitle("learn-agent · 正在取消任务并保存会话……"); win.setEnabled(false); }
    if (settingsWindow && !settingsWindow.isDestroyed()) settingsWindow.setEnabled(false);
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
    const override = workspaceArg < 0 ? undefined : process.argv[workspaceArg + 1];
    if (workspaceArg >= 0 && !override) throw new Error("--workspace 后需要提供项目目录。");
    prepareDemo(paths);
    settings = await new Settings(app.getPath("userData"), electron.safeStorage,
      { workspace: paths.workspace }).load(override && path.resolve(override));
    const themeFile = path.join(app.getPath("userData"), "theme.json");
    themes = { value: "system", save(value) {
      fs.writeFileSync(themeFile, JSON.stringify({ theme: value })); this.value = value;
    } };
    try {
      const saved = JSON.parse(fs.readFileSync(themeFile, "utf8")).theme;
      if (["system", "light", "dark"].includes(saved)) themes.value = saved;
    } catch { /* first launch has no stored desktop theme */ }
    if (quitting) return;
    if (!settings.ready) { showSettings(); return; }
    try { await restartBackend(settings.data.workspace); }
    catch (error) { showSettings(error.message); }
  });
  startup.catch(fail);
}

if (process.versions.electron) startDesktop(require("electron"));
module.exports = { chooseProject, createWindow, startDesktop };
