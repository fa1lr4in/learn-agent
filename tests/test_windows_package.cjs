// Run explicitly after building, or set LEARN_AGENT_PACKAGE_RESOURCES to an installed resources folder.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { createHash } = require("node:crypto");
const asar = require("../desktop/node_modules/@electron/asar");
const { setTimeout: delay } = require("node:timers/promises");
const { Backend } = require("../desktop/backend.cjs");

test("shipping Python and API run tools, persist and reopen with no developer paths", async t => {
  const resources = path.resolve(process.env.LEARN_AGENT_PACKAGE_RESOURCES || path.join(__dirname, "../dist/win-unpacked/resources"));
  const bundled = path.join(resources, "backend"), python = path.join(bundled, "python/python.exe");
  assert.ok(fs.existsSync(python), "Build Windows first, or point LEARN_AGENT_PACKAGE_RESOURCES at an installed app.");
  const manifest = JSON.parse(fs.readFileSync(path.join(resources, "build-manifest.json"), "utf8"));
  const appPackage = JSON.parse(asar.extractFile(path.join(resources, "app.asar"), "package.json").toString("utf8"));
  assert.equal(appPackage.version, manifest.version);
  assert.match(manifest.source.commit, /^[0-9a-f]{40}$/);
  assert.equal(manifest.source.dirty, false);
  const sourceFiles = Object.fromEntries(Object.entries(manifest.source.files).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0));
  assert.equal(createHash("sha256").update(JSON.stringify(sourceFiles)).digest("hex"), manifest.source.sha256);
  for (const [relative, expected] of Object.entries(manifest.files)) {
    const file = path.resolve(resources, relative);
    assert.ok(file.startsWith(resources + path.sep));
    const bytes = relative.startsWith("app/") ?
      asar.extractFile(path.join(resources, "app.asar"), relative.slice(4)) : fs.readFileSync(file);
    assert.equal(createHash("sha256").update(bytes).digest("hex"), expected, relative);
    const sourcePath = relative.startsWith("backend/") ? relative.slice(8) :
      relative.startsWith("app/desktop/") || relative.startsWith("app/web/") ? relative.slice(4) :
      relative.startsWith("demo_project/") ? `desktop/${relative}` : relative.startsWith("app/") ? relative.slice(4) : null;
    if (sourcePath && manifest.source.files[sourcePath]) assert.equal(expected, manifest.source.files[sourcePath], sourcePath);
  }
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "learn-agent-package-"));
  const project = path.join(root, "中文 project"), logs = path.join(root, "用户 logs");
  fs.cpSync(path.join(resources, "demo_project"), project, { recursive: true });
  // A test-only entry imports the shipping modules. No offline bypass is added to the distributed app.
  const fixture = fs.readFileSync(path.join(__dirname, "desktop_backend_fixture.py"), "utf8");
  fs.writeFileSync(path.join(root, "api.py"), `import sys\nsys.path.insert(0,${JSON.stringify(bundled)})\n${fixture}`);
  const env = { ...process.env, PATH: path.join(process.env.SystemRoot, "System32"),
    DEEPSEEK_API_KEY: "offline-package-only", PYTHONHOME: "invalid-developer-runtime", PYTHONPATH: "invalid-source-path" };
  const options = { root, python, logDirectory: logs, env };
  let backend = new Backend(options);
  t.after(async () => {
    await backend.stop();
    assert.equal(path.dirname(root), os.tmpdir());
    fs.rmSync(root, { recursive: true, force: true });
  });
  const probe = spawnSync(python, ["-I", "-B", "-X", "utf8", "-c", "import sys,fastapi,sqlite3,ssl; print(sys.prefix); print(fastapi.__file__)"],
    { env: { ...env, PYTHONHOME: "", PYTHONPATH: "" }, encoding: "utf8", windowsHide: true });
  assert.equal(probe.status, 0, probe.stderr);
  assert.equal(probe.stdout.toLowerCase().includes("anaconda"), false);
  assert.ok(probe.stdout.includes(path.join(bundled, "python")));
  const request = async (info, route, body) => {
    const response = await fetch(info.url + route, { headers: { Authorization: `Bearer ${info.token}`, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { method: "POST", body: JSON.stringify(body) }) });
    assert.ok(response.ok, await response.clone().text()); return response.json();
  };
  const info = await backend.start(project);
  const page = await fetch(info.url + "/"); assert.equal(page.status, 200);
  const session = await request(info, "/sessions", {});
  const task = await request(info, `/sessions/${session.id}/tasks`, { prompt: "检查项目" });
  let previous, snapshot;
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    snapshot = await request(info, `/tasks/${task.id}`);
    if (snapshot.finished) break;
    const approval = snapshot.pending_approval;
    if (approval && approval.id !== previous) {
      await request(info, `/tasks/${task.id}/approvals`, { approval_id: approval.id, approved: true });
      previous = approval.id;
    }
    await delay(30);
  }
  assert.equal(snapshot.finished, true); assert.equal(snapshot.reason, "stop"); assert.equal(snapshot.session_saved, true);
  const results = snapshot.events.filter(e => e.event === "tool_result");
  assert.equal(results.length, 2); assert.ok(results.every(e => e.result.ok));
  assert.match(results[0].result.stderr, /Ran 4 tests[\s\S]*OK/);
  assert.match(results[1].result.stdout, /'total': 42, 'average': 14.0/);
  assert.equal(snapshot.events.filter(e => e.event === "tool_approval" && e.source === "user").length, 2);
  await backend.stop();
  backend = new Backend(options);
  const restarted = await backend.start(project);
  assert.equal((await request(restarted, `/sessions/${session.id}`)).reason, "stop");
  assert.deepEqual(await request(restarted, "/tasks"), []);
  t.diagnostic("Shipping runtime: 4 tests OK, total=42, average=14.0; 2 approvals; session restored; no model API called.");
});
