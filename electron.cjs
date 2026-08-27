const { app, BrowserWindow, ipcMain, shell } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");

app.commandLine.appendSwitch("enable-transparent-visuals");

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
  process.exit(0);
}

process.on("uncaughtException", (err) => {
  console.error("[FATAL] Uncaught exception:", err);
});
process.on("unhandledRejection", (reason) => {
  console.error("[FATAL] Unhandled rejection:", reason);
});

let mainWindow = null;
let pythonProcess = null;
let isQuitting = false;

function findIndexHtml() {
  const candidates = [
    path.join(process.resourcesPath, "app", "dist", "index.html"),
    path.join(process.resourcesPath, "dist", "index.html"),
    path.join(__dirname, "dist", "index.html"),
    path.join(app.getAppPath(), "dist", "index.html"),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  console.error("[Electron] Could not find dist/index.html — did you run `npm run build`?");
  return null;
}

function startPython() {
  // Prefer the project's own venv interpreter over a bare system one so
  // jarvis.py actually has its pip-installed dependencies available.
  const isWin = process.platform === "win32";
  const venvPythonw = path.join(__dirname, "venv", "Scripts", "pythonw.exe");
  const venvPython = path.join(__dirname, "venv", "bin", "python");
  let pythonCmd;
  if (isWin && fs.existsSync(venvPythonw)) pythonCmd = venvPythonw;
  else if (!isWin && fs.existsSync(venvPython)) pythonCmd = venvPython;
  else pythonCmd = isWin ? "pythonw" : "python3";
  const scriptPath = path.join(__dirname, "jarvis.py");

  if (!fs.existsSync(scriptPath)) {
    console.error("[Python] jarvis.py not found next to electron.cjs");
    return;
  }

  pythonProcess = spawn(pythonCmd, [scriptPath], {
    cwd: __dirname,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  });

  pythonProcess.stdout?.on("data", d => console.log("[Python]", d.toString()));
  pythonProcess.stderr?.on("data", d => console.error("[Python:err]", d.toString()));
  pythonProcess.on("error", e => console.error("[Python launch error]", e));
  pythonProcess.on("exit", c => console.log("[Python] exited with code", c));
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 480,
    minHeight: 360,
    frame: false,
    transparent: true,
    hasShadow: false,
    show: false,
    resizable: true,
    title: "J.A.R.V.I.S",
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, "preload.cjs"),
    },
  });

  const indexHtml = findIndexHtml();
  if (indexHtml) {
    mainWindow.loadFile(indexHtml);
  } else {
    mainWindow.loadURL(
      "data:text/html," +
      "<html><body style='background:%230d0d1a;color:%2300f0ff;" +
      "font-family:monospace;display:flex;align-items:center;" +
      "justify-content:center;height:100vh;margin:0;" +
      "flex-direction:column;gap:20px;'>" +
      "<div style='font-size:24px;letter-spacing:4px'>J.A.R.V.I.S</div>" +
      "<div style='font-size:12px;opacity:0.6'>ERROR: dist/index.html not found</div>" +
      "<div style='font-size:10px;opacity:0.4'>Run: npm run build</div>" +
      "</body></html>"
    );
  }

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  mainWindow.on("close", (e) => {
    if (isQuitting) return;
    e.preventDefault();
    isQuitting = true;
    // Deliberately does NOT kill pythonProcess anymore. jarvis.py is now a
    // persistent background service (voice, texting, email watching, the
    // reminder clock) meant to keep running whether or not this window is
    // open -- see the JarvisBackend scheduled task in the README. Closing
    // the HUD just closes the visual window.
    mainWindow.webContents.send("app-closing");
    setTimeout(() => app.quit(), 300);
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
}

ipcMain.on("window-minimise", () => mainWindow?.minimize());
ipcMain.on("window-maximise", () => {
  if (mainWindow?.isMaximized()) mainWindow.unmaximize();
  else mainWindow?.maximize();
});
ipcMain.on("window-close", () => mainWindow?.close());

app.whenReady().then(() => {
  startPython();
  createWindow();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
