const path = require("node:path");

module.exports = {
  appId: "io.github.fa1lr4in.learnagent", productName: "learn-agent",
  directories: { app: "../build/windows/app", output: "../dist" },
  electronVersion: "45.0.0", electronDist: path.join(__dirname, "node_modules/electron/dist"),
  asar: true,
  files: ["package.json", "desktop/*", "web/*", "LICENSE"],
  extraResources: [
    { from: "../build/windows/backend", to: "backend" },
    { from: "../build/windows/demo_project", to: "demo_project" },
    { from: "../build/windows/manifest.json", to: "build-manifest.json" }
  ],
  win: { target: [{ target: "nsis", arch: ["x64"] }], signExecutable: false },
  nsis: {
    oneClick: false, perMachine: false, allowElevation: false, allowToChangeInstallationDirectory: true,
    deleteAppDataOnUninstall: false, runAfterFinish: false,
    artifactName: "learn-agent-Setup-${version}-${arch}.${ext}"
  }
};
