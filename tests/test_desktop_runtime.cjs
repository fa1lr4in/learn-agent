const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { runtimePaths, prepareDemo } = require("../desktop/runtime.cjs");
const { Backend } = require("../desktop/backend.cjs");

test("development keeps the repository runtime, logs and project paths", () => {
  const result = runtimePaths({ isPackaged: false });
  assert.equal(result.root, path.resolve(__dirname, ".."));
  assert.equal(result.logDirectory, path.join(result.root, "logs"));
  assert.equal(result.workspace, path.join(result.root, "workspace/demo_project"));
  assert.equal(result.template, null);
});

test("packaged resources are read-only inputs and user data is outside the installation", t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "learn-agent-runtime-"));
  t.after(() => {
    assert.equal(path.dirname(root), os.tmpdir());
    fs.rmSync(root, { recursive: true, force: true });
  });
  const resources = path.join(root, "安装 resources"), profile = path.join(root, "用户 profile");
  const result = runtimePaths({ isPackaged: true, getPath: () => profile }, resources);
  assert.equal(result.python, path.join(resources, "backend/python/python.exe"));
  assert.equal(result.logDirectory, path.join(profile, "logs"));
  fs.mkdirSync(result.template, { recursive: true });
  fs.writeFileSync(path.join(result.template, "main.py"), "print(42)");
  prepareDemo(result);
  const projectFile = path.join(result.workspace, "main.py");
  assert.equal(fs.readFileSync(projectFile, "utf8"), "print(42)");
  fs.writeFileSync(projectFile, "print(99)");
  prepareDemo(result);
  assert.equal(fs.readFileSync(projectFile, "utf8"), "print(99)");
  fs.unlinkSync(projectFile);
  prepareDemo(result);
  assert.equal(fs.existsSync(projectFile), false); // never restore a user's intentionally deleted file
  const backend = new Backend({ ...result, env: { PYTHONHOME: "host", PYTHONPATH: "host", DEEPSEEK_API_KEY: "test-key" } });
  assert.equal(backend.record, path.join(profile, "logs/.desktop-server.json"));
  assert.equal(backend.env.PYTHONHOME, undefined);
  assert.equal(backend.env.PYTHONPATH, undefined);
  assert.equal(backend.env.DEEPSEEK_API_KEY, "test-key");
});
