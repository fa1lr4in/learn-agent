const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

function createSettingsWindow(electron, { parent, settings, save, theme, diagnostics, closed, notice = "" }) {
  const { BrowserWindow, ipcMain, dialog, shell } = electron;
  const file = path.join(__dirname, "settings.html"), origin = pathToFileURL(file).href;
  const win = new BrowserWindow({ title: "learn-agent · 设置", width: 680, height: 760, minWidth: 560, minHeight: 600,
    show: false, ...(parent ? { parent, modal: true } : {}), backgroundColor: "#1b1b1b",
    webPreferences: { preload: path.join(__dirname, "settings-preload.cjs"), nodeIntegration: false,
      contextIsolation: true, sandbox: true, webSecurity: true, webviewTag: false } });
  win.setMenu(null);
  win.webContents.on("will-navigate", event => event.preventDefault());
  win.webContents.on("will-redirect", event => event.preventDefault());
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.session.setPermissionRequestHandler((_contents, _permission, answer) => answer(false));
  win.webContents.session.setPermissionCheckHandler(() => false);
  let busy = false;
  const handlers = {
    read: () => ({ ...settings.view(), warning: settings.warning || notice, theme, initial: !parent }),
    choose: async () => {
      const result = await dialog.showOpenDialog(win, { title: "选择项目目录", defaultPath: settings.data.workspace,
        properties: ["openDirectory"] });
      return result.canceled ? null : result.filePaths[0];
    },
    save: async input => { await save(input); if (!win.isDestroyed()) win.close(); },
    logs: async () => {
      const folder = path.join(__dirname, "..", "logs");
      fs.mkdirSync(folder, { recursive: true });
      if (await shell.openPath(folder)) throw new Error("无法打开日志目录，请在项目根目录查看 logs。");
    },
    diagnostics,
    cancel: () => win.close()
  };
  for (const [name, handler] of Object.entries(handlers)) {
    ipcMain.handle(`settings:${name}`, async (event, ...args) => {
      if (event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame ||
          event.senderFrame.url !== origin || args.length !== (name === "save" ? 1 : 0)) {
        throw new Error("不允许的设置请求。");
      }
      if (busy) throw new Error("正在处理设置，请稍候。");
      busy = true;
      try { return await handler(...args); } finally { busy = false; }
    });
  }
  win.once("ready-to-show", () => win.show());
  win.on("closed", () => {
    for (const name of Object.keys(handlers)) ipcMain.removeHandler(`settings:${name}`);
    closed();
  });
  return { win, loaded: win.loadFile(file) };
}

module.exports = { createSettingsWindow };
