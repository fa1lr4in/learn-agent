const { contextBridge, ipcRenderer } = require("electron");

// 页面只获得这一项能力，不接收通用 IPC、文件或命令执行接口。
contextBridge.exposeInMainWorld("learnAgentDesktop", {
  chooseProject: () => ipcRenderer.invoke("project:choose")
});
