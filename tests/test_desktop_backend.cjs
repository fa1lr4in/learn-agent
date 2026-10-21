const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const { spawnSync } = require("node:child_process");
const { setTimeout: delay } = require("node:timers/promises");
const { Backend } = require("../desktop/backend.cjs");

const repo = path.resolve(__dirname, "..");
const key = "offline-only-key-for-lifecycle-tests";

function fixture(t, options = {}) {
  const root = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), "learn-agent-lifecycle-")));
  const project = path.join(root, "中文 project"); fs.mkdirSync(project);
  fs.symlinkSync(path.join(repo, ".venv"), path.join(root, ".venv"), process.platform === "win32" ? "junction" : "dir");
  fs.writeFileSync(path.join(root, "api.py"), `import sys,runpy\nsys.path.insert(0,${JSON.stringify(repo)})\nrunpy.run_path(${JSON.stringify(path.join(repo, "tests/desktop_backend_fixture.py"))},run_name='__main__')\n`);
  fs.writeFileSync(path.join(project, "main.py"), "print(42)\n");
  fs.writeFileSync(path.join(project, "test_stats.py"), "import unittest\nclass TestStats(unittest.TestCase):\n    def test_ok(self): self.assertEqual(6*7,42)\n");
  const backend = new Backend({ root, env: { ...process.env, DEEPSEEK_API_KEY: key }, startupMs: 10000, ...options });
  t.after(async () => {
    await backend.stop();
    if (!t.passed && fs.existsSync(backend.diagnostics)) t.diagnostic(fs.readFileSync(backend.diagnostics, "utf8"));
    assert.ok(root.startsWith(fs.realpathSync.native(os.tmpdir()) + path.sep));
    fs.rmSync(root, { recursive: true, force: true });
  });
  return { root, project, backend };
}

async function request(info, route, body) {
  const response = await fetch(info.url + route, { headers: { Authorization: `Bearer ${info.token}`, "Content-Type": "application/json" },
    ...(body === undefined ? {} : { method: "POST", body: JSON.stringify(body) }) });
  assert.ok(response.ok, await response.clone().text());
  return response.json();
}

async function until(check) {
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) { const value = await check(); if (value) return value; await delay(30); }
  assert.fail("timed out waiting for lifecycle evidence");
}

test("owned dynamic server ignores a stale record and an occupied default port", async t => {
  const { root, project, backend } = fixture(t);
  const occupied = net.createServer();
  await new Promise((resolve, reject) => { occupied.once("error", error => error.code === "EADDRINUSE" ? resolve() : reject(error)); occupied.listen(8000, "127.0.0.1", resolve); });
  t.after(() => occupied.close());
  fs.mkdirSync(path.join(root, "logs"));
  const stale = '{"url":"http://127.0.0.1:8000","instance_id":"someone-else"}';
  fs.writeFileSync(path.join(root, "logs/.api-server.json"), stale);
  const info = await backend.start(project);
  assert.notEqual(new URL(info.url).port, "8000");
  assert.ok(info.pid === backend.child.pid || info.parent_pid === backend.child.pid);
  assert.equal((await request(info, "/instance")).instance_id, info.instance_id);
  assert.equal(fs.readFileSync(path.join(root, "logs/.api-server.json"), "utf8"), stale);
  assert.equal((await fetch(info.url + "/instance")).status, 401);
  assert.equal((await fetch(info.url + "/sessions", { method: "POST", headers: { Authorization: `Bearer ${info.token}`, Origin: "http://127.0.0.1:8000" } })).status, 403);
  assert.deepEqual(await backend.stop(), { forced: false });
  await assert.rejects(fetch(info.url + "/health"));
  assert.equal(fs.existsSync(backend.record), false);
  assert.equal(fs.readFileSync(path.join(root, "logs/.api-server.json"), "utf8"), stale);
});

test("backend accepts a Windows short path for the selected project", { skip: process.platform !== "win32" }, async t => {
  const { project, backend } = fixture(t);
  const code = "import ctypes,sys; b=ctypes.create_unicode_buffer(32768); " +
    "n=ctypes.windll.kernel32.GetShortPathNameW(sys.argv[1],b,len(b)); assert n; print(b.value)";
  const result = spawnSync(path.join(repo, ".venv/Scripts/python.exe"), ["-B", "-X", "utf8", "-c", code, project],
    { encoding: "utf8", windowsHide: true, env: { ...process.env, DEEPSEEK_API_KEY: "" } });
  assert.equal(result.status, 0, result.stderr);
  const shortPath = result.stdout.trim();
  if (shortPath.toLowerCase() === fs.realpathSync.native(project).toLowerCase()) return t.skip("No distinct short path");
  const info = await backend.start(shortPath);
  assert.equal(info.workspace.toLowerCase(), fs.realpathSync.native(project).toLowerCase());
});

test("closing at approval persists cancellation without executing either tool or requesting another reply", async t => {
  const { root, project, backend } = fixture(t);
  const info = await backend.start(project);
  const session = await request(info, "/sessions", {});
  const task = await request(info, `/sessions/${session.id}/tasks`, { prompt: "检查项目" });
  await until(async () => (await request(info, `/tasks/${task.id}`)).pending_approval);
  assert.deepEqual(await backend.stop(), { forced: false });
  const log = fs.readdirSync(path.join(root, "logs")).find(name => name.endsWith(".jsonl"));
  const events = fs.readFileSync(path.join(root, "logs", log), "utf8").trim().split("\n").map(JSON.parse);
  assert.equal(events.at(-1).reason, "cancelled");
  assert.equal(events.filter(e => e.event === "tool_result").length, 0);
  assert.equal(events.filter(e => e.event === "model_response").length, 1);
  const restarted = new Backend({ root, env: { ...process.env, DEEPSEEK_API_KEY: key } });
  try {
    const next = await restarted.start(project);
    assert.notEqual(info.instance_id, next.instance_id);
    assert.notEqual(info.token, next.token);
    const saved = await request(next, `/sessions/${session.id}`);
    assert.equal(saved.reason, "cancelled");
    assert.ok(saved.messages.some(m => m.role === "assistant"));
    assert.deepEqual(await request(next, "/tasks"), []);
  } finally { await restarted.stop(); }
});

test("two real executions have separate approvals and saved history survives restart", async t => {
  const { root, project, backend } = fixture(t);
  const info = await backend.start(project);
  const session = await request(info, "/sessions", {});
  const task = await request(info, `/sessions/${session.id}/tasks`, { prompt: "检查项目" });
  let previous;
  for (let i = 0; i < 2; i++) {
    const approval = await until(async () => {
      const pending = (await request(info, `/tasks/${task.id}`)).pending_approval;
      return pending && pending.id !== previous ? pending : null;
    });
    assert.equal(approval.tool, i === 0 ? "run_command" : "run_python");
    await request(info, `/tasks/${task.id}/approvals`, { approval_id: approval.id, approved: true });
    previous = approval.id;
  }
  const finished = await until(async () => { const snapshot = await request(info, `/tasks/${task.id}`); return snapshot.finished && snapshot; });
  assert.equal(finished.reason, "stop"); assert.equal(finished.session_saved, true);
  assert.equal(finished.summary.usage.total_tokens, 24);
  assert.equal(finished.events.find(e => e.event === "tool_result" && e.tool === "run_python").result.stdout.trim(), "42");
  await backend.stop();
  assert.ok(fs.statSync(path.join(root, "logs/sessions.sqlite3")).size > 0);
});

test("startup failures leave no owned child, no secret diagnostics and no usable record", async t => {
  const { root, project, backend } = fixture(t, { env: { ...process.env, DEEPSEEK_API_KEY: "" } });
  await assert.rejects(backend.start(project), /模型密钥/);
  assert.equal(backend.exited, true);
  assert.equal(fs.existsSync(backend.record), false);
  assert.match(fs.readFileSync(backend.diagnostics, "utf8"), /DEEPSEEK_API_KEY/);
  backend.env.DEEPSEEK_API_KEY = key; backend.stderr = key;
  backend.diagnose(key);
  assert.equal(fs.readFileSync(backend.diagnostics, "utf8").includes(key), false);
  assert.ok(fs.existsSync(root));
});

test("losing the parent control pipe also closes the service and persists cancellation", async t => {
  let unexpected = 0;
  const { root, project, backend } = fixture(t, { onUnexpectedExit: () => { unexpected++; } });
  const info = await backend.start(project);
  const session = await request(info, "/sessions", {});
  const task = await request(info, `/sessions/${session.id}/tasks`, { prompt: "检查项目" });
  await until(async () => (await request(info, `/tasks/${task.id}`)).pending_approval);
  backend.child.stdin.destroy(); // same EOF observed when the parent's handle disappears
  await until(() => backend.exited);
  assert.equal(unexpected, 1);
  const log = fs.readdirSync(path.join(root, "logs")).find(name => name.endsWith(".jsonl"));
  const events = fs.readFileSync(path.join(root, "logs", log), "utf8").trim().split("\n").map(JSON.parse);
  assert.equal(events.at(-1).reason, "cancelled");
  assert.equal(events.filter(e => e.event === "tool_result").length, 0);
  await assert.rejects(fetch(info.url + "/health"));
});

test("a stuck foreground command and its child are removed by the owned tree fallback", async t => {
  const { project, backend } = fixture(t, { shutdownMs: 300 });
  fs.writeFileSync(path.join(project, "main.py"), "import os,sys,subprocess,time,json\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\nopen('pids.json','w').write(json.dumps([os.getpid(),p.pid]))\ntime.sleep(120)\n");
  const info = await backend.start(project);
  const session = await request(info, "/sessions", {});
  const task = await request(info, `/sessions/${session.id}/tasks`, { prompt: "[hang]" });
  const approval = await until(async () => (await request(info, `/tasks/${task.id}`)).pending_approval);
  await request(info, `/tasks/${task.id}/approvals`, { approval_id: approval.id, approved: true });
  await until(() => fs.existsSync(path.join(project, "pids.json")));
  const pids = JSON.parse(fs.readFileSync(path.join(project, "pids.json"), "utf8"));
  assert.deepEqual(await backend.stop(), { forced: true });
  for (const pid of [info.pid, ...pids]) await until(() => { try { process.kill(pid, 0); return false; } catch { return true; } });
  assert.match(fs.readFileSync(backend.diagnostics, "utf8"), /强制/);
  assert.equal(fs.existsSync(backend.record), false);
});
