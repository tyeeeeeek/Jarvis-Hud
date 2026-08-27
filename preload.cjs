const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  minimise: () => ipcRenderer.send("window-minimise"),
  maximise: () => ipcRenderer.send("window-maximise"),
  close: () => ipcRenderer.send("window-close"),
  onClose: (callback) => ipcRenderer.on("app-closing", callback),
});
