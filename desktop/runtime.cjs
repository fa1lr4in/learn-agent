const fs = require("node:fs");
const path = require("node:path");

function runtimePaths(app, resources = process.resourcesPath) {
  const root = app.isPackaged ? path.join(resources, "backend") : path.join(__dirname, "..");
  return {
    root,
    python: app.isPackaged ? path.join(root, "python", "python.exe") :
      path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python"),
    logDirectory: path.join(app.isPackaged ? app.getPath("userData") : root, "logs"),
    workspace: app.isPackaged ? path.join(app.getPath("userData"), "projects", "demo_project") :
      path.join(root, "workspace", "demo_project"),
    template: app.isPackaged ? path.join(resources, "demo_project") : null
  };
}

function prepareDemo(paths) {
  if (paths.template && !fs.existsSync(paths.workspace)) {
    fs.mkdirSync(path.dirname(paths.workspace), { recursive: true });
    fs.cpSync(paths.template, paths.workspace, { recursive: true, force: false });
  }
}

module.exports = { runtimePaths, prepareDemo };
