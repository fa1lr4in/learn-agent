const { contextBridge, ipcRenderer } = require("electron");

// 页面只获得目录选择与主题偏好，不接收通用 IPC、文件或命令执行接口。
contextBridge.exposeInMainWorld("learnAgentDesktop", {
  chooseProject: () => ipcRenderer.invoke("project:choose"),
  getTheme: () => ipcRenderer.sendSync("theme:get"),
  setTheme: value => ipcRenderer.invoke("theme:set", value)
});
