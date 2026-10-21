const { contextBridge, ipcRenderer } = require("electron");

// No credential readback, generic IPC, filesystem or shell capability is exposed.
contextBridge.exposeInMainWorld("learnAgentSettings", {
  read: () => ipcRenderer.invoke("settings:read"),
  chooseProject: () => ipcRenderer.invoke("settings:choose"),
  save: input => ipcRenderer.invoke("settings:save", input),
  openLogs: () => ipcRenderer.invoke("settings:logs"),
  diagnostics: () => ipcRenderer.invoke("settings:diagnostics"),
  cancel: () => ipcRenderer.invoke("settings:cancel")
});
