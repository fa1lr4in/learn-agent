const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const vm = require("node:vm");
const { randomBytes } = require("node:crypto");
const { pathToFileURL } = require("node:url");
const { Settings } = require("../desktop/settings.cjs");
const { createSettingsWindow } = require("../desktop/settings-window.cjs");

const key = "offline-only-settings-credential";

function fixture(t, env = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "learn-agent-settings-"));
  const project = path.join(root, "中文 project"); fs.mkdirSync(project);
  const ciphertexts = new Map();
  const crypto = {
    async isAsyncEncryptionAvailable() { return true; },
    async encryptStringAsync(value) { const bytes = randomBytes(64); ciphertexts.set(bytes.toString("base64"), value); return bytes; },
    async decryptStringAsync(bytes) { const result = ciphertexts.get(bytes.toString("base64")); if (!result) throw Error("unavailable"); return { result }; }
  };
  const store = new Settings(root, crypto, { workspace: project, env });
  t.after(() => { assert.equal(path.dirname(root), os.tmpdir()); fs.rmSync(root, { recursive: true, force: true }); });
  const input = { workspace: project, model: "deepseek-flash", apiKey: key };
  return { root, project, crypto, store, input };
}

test("first launch has no configuration and never exposes an environment credential", async t => {
  const { store } = fixture(t, { DEEPSEEK_API_KEY: key });
  await store.load();
  assert.equal(store.ready, false); assert.equal(store.view().keySource, "environment");
  assert.equal(JSON.stringify(store.view()).includes(key), false);
  assert.equal(JSON.stringify(store).includes(key), false);
});

test("an existing environment key can be saved once and restored without that environment", async t => {
  const { root, project, crypto, store, input } = fixture(t, { DEEPSEEK_API_KEY: key });
  await store.load(); await store.save({ ...input, apiKey: "" });
  const contents = fs.readFileSync(store.file, "utf8");
  assert.equal(contents.includes(key), false);
  assert.deepEqual(Object.keys(JSON.parse(contents)).sort(), ["encryptedKey", "model", "version", "workspace"]);
  const next = await new Settings(root, crypto, { workspace: project, env: {} }).load();
  assert.equal(next.ready, true); assert.equal(next.view().keySource, "saved");
  assert.equal(next.environment().DEEPSEEK_API_KEY, key);
  assert.equal(next.environment().DEEPSEEK_MODEL, "deepseek-flash");
  assert.equal(JSON.stringify(next.view()).includes(JSON.parse(contents).encryptedKey), false);
});

test("the saved key takes precedence over an unrelated inherited key", async t => {
  const { root, project, crypto, store, input } = fixture(t);
  await store.load(); await store.save(input);
  const next = await new Settings(root, crypto, { workspace: project, env: { DEEPSEEK_API_KEY: "different-account" } }).load();
  assert.equal(next.environment().DEEPSEEK_API_KEY, key);
  await next.save({ ...input, model: "another-model", apiKey: "" });
  assert.equal(next.environment().DEEPSEEK_API_KEY, key);
  assert.equal(next.environment().DEEPSEEK_MODEL, "another-model");
});

test("missing credentials and unavailable encryption never create a plaintext fallback", async t => {
  const { store, input, crypto } = fixture(t);
  await store.load();
  await assert.rejects(store.save({ ...input, apiKey: "" }), /API Key/);
  crypto.isAsyncEncryptionAvailable = async () => false;
  await assert.rejects(store.save(input), /加密暂不可用/);
  crypto.isAsyncEncryptionAvailable = async () => true;
  crypto.encryptStringAsync = async () => { throw Error(key); };
  await assert.rejects(store.save(input), error => !error.message.includes(key));
  assert.equal(fs.existsSync(store.file), false);
});

test("invalid configuration cannot overwrite the previous working settings", async t => {
  const { root, store, input } = fixture(t);
  await store.load(); await store.save(input);
  const before = fs.readFileSync(store.file, "utf8");
  for (const changed of [{ model: "" }, { model: "bad\nmodel" }, { workspace: "relative" },
    { workspace: path.join(root, "missing") }, { workspace: store.file }, { apiKey: "bad\nheader" },
    { apiKey: 123 }, { apiKey: "x".repeat(513) }, { extra: "not allowed" }]) {
    await assert.rejects(store.save({ ...input, ...changed }));
    assert.equal(fs.readFileSync(store.file, "utf8"), before);
  }
});

test("a failed atomic replacement keeps the old key and settings usable", async t => {
  const { store, input, root } = fixture(t);
  await store.load(); await store.save(input);
  const before = fs.readFileSync(store.file, "utf8"), rename = fs.renameSync;
  fs.renameSync = () => { throw Error("locked"); };
  try { await assert.rejects(store.save({ ...input, apiKey: "replacement" }), /无法保存/); }
  finally { fs.renameSync = rename; }
  assert.equal(fs.readFileSync(store.file, "utf8"), before);
  assert.equal(store.environment().DEEPSEEK_API_KEY, key);
  assert.equal(fs.readdirSync(root).some(name => name.endsWith(".tmp")), false);
});

test("unreadable credentials require repair before the backend can start", async t => {
  const { root, project, store, input, crypto } = fixture(t);
  await store.load(); await store.save(input);
  crypto.decryptStringAsync = async () => { throw Error(key); };
  const next = await new Settings(root, crypto, { workspace: project, env: {} }).load();
  assert.equal(next.ready, false); assert.match(next.view().warning, /解密/);
  assert.equal(JSON.stringify(next.view()).includes(key), false);
  await next.save({ ...input, apiKey: "new-valid-credential" });
  assert.equal(next.ready, true);
});

test("malformed files and unknown versions return a fixed repair message without secret snippets", async t => {
  const { store, root, project, crypto, input } = fixture(t);
  for (const text of [key, JSON.stringify({ ...input, version: 999 }), JSON.stringify({ version: 1, ...input, encryptedKey: "" })]) {
    fs.writeFileSync(store.file, text);
    const next = await new Settings(root, crypto, { workspace: project, env: {} }).load();
    assert.equal(next.ready, false); assert.ok(next.view().warning);
    assert.equal(JSON.stringify(next.view()).includes(key), false);
  }
});

test("project memory persists independently and explicit startup paths take precedence", async t => {
  const { root, project, store, input, crypto } = fixture(t);
  await store.load(); await store.save(input);
  const other = path.join(root, "other"); fs.mkdirSync(other);
  store.rememberProject(other);
  const next = await new Settings(root, crypto, { workspace: project, env: {} }).load();
  assert.equal(next.view().workspace, fs.realpathSync(other));
  await next.load(project);
  assert.equal(next.view().workspace, fs.realpathSync(project));
  assert.equal(JSON.parse(fs.readFileSync(store.file, "utf8")).workspace, fs.realpathSync(other));
});

test("missing remembered projects are recoverable and diagnostics redact both current and inherited keys", async t => {
  const { root, project, store, input, crypto } = fixture(t, { DEEPSEEK_API_KEY: "inherited-secret" });
  await store.load(); await store.save(input);
  const data = JSON.parse(fs.readFileSync(store.file, "utf8")); data.workspace = path.join(root, "removed");
  fs.writeFileSync(store.file, JSON.stringify(data));
  const next = await new Settings(root, crypto, { workspace: project, env: {} }).load();
  assert.equal(next.ready, false); assert.match(next.view().warning, /目录/);
  assert.equal(store.redact(`${key} inherited-secret`), "[redacted] [redacted]");
});

test("settings preload has fixed operations and never offers credential readback or arbitrary IPC", async () => {
  let bridge, call;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../desktop/settings-preload.cjs"), "utf8"), {
    require() { return { contextBridge: { exposeInMainWorld(name, value) { assert.equal(name, "learnAgentSettings"); bridge = value; } },
      ipcRenderer: { invoke(...args) { call = args; return Promise.resolve(); } } }; }
  });
  assert.deepEqual(Object.keys(bridge), ["read", "chooseProject", "save", "openLogs", "diagnostics", "cancel"]);
  for (const [method, channel] of [["read", "read"], ["chooseProject", "choose"], ["openLogs", "logs"], ["diagnostics", "diagnostics"], ["cancel", "cancel"]]) {
    await bridge[method]("ignored-path"); assert.deepEqual(call, [`settings:${channel}`]);
  }
  await bridge.save({ model: "model" }); assert.equal(call[0], "settings:save");
});

test("only the isolated settings main frame may read, save or open the fixed logs path", async t => {
  const { store } = fixture(t);
  await store.load();
  const handlers = {}, events = {}; let options, opened, saved;
  class Window {
    constructor(value) { options = value; this.webContents = { mainFrame: { url: pathToFileURL(path.join(__dirname, "../desktop/settings.html")).href },
      on(name, callback) { events[name] = callback; }, setWindowOpenHandler() {},
      session: { setPermissionRequestHandler() {}, setPermissionCheckHandler() {} } }; }
    setMenu() {} once() {} on(name, callback) { events[name] = callback; } show() {} close() {} isDestroyed() { return false; }
    loadFile() { return Promise.resolve(); }
  }
  const { win } = createSettingsWindow({ BrowserWindow: Window, ipcMain: {
    handle(name, callback) { handlers[name] = callback; }, removeHandler(name) { delete handlers[name]; }
  }, dialog: { showOpenDialog: async () => ({ canceled: true }) }, shell: { openPath: async value => { opened = value; return ""; } } },
  { settings: store, theme: "dark", diagnostics: () => ({ status: "offline" }), save: async input => { saved = input; }, closed() {} });
  assert.equal(options.webPreferences.nodeIntegration, false); assert.equal(options.webPreferences.sandbox, true);
  const event = { sender: win.webContents, senderFrame: win.webContents.mainFrame };
  assert.equal((await handlers["settings:read"](event)).keySource, "missing");
  for (const invalid of [{ ...event, sender: {} }, { ...event, senderFrame: {} }, { ...event, senderFrame: null }]) {
    for (const name of ["read", "choose", "logs", "diagnostics", "save"]) {
      await assert.rejects(handlers[`settings:${name}`](invalid, ...(name === "save" ? [{}] : [])));
    }
  }
  await assert.rejects(handlers["settings:logs"](event, "arbitrary-folder"));
  await handlers["settings:logs"](event); assert.equal(opened, path.resolve(__dirname, "../logs"));
  assert.equal(await handlers["settings:choose"](event), null);
  await handlers["settings:save"](event, { model: "m" }); assert.deepEqual(saved, { model: "m" });
  let prevented = false; events["will-navigate"]({ preventDefault() { prevented = true; } }); assert.equal(prevented, true);
  events.closed(); assert.deepEqual(handlers, {});
});

test("first-use save starts the backend, and an unfinished task blocks configuration changes", async t => {
  const { root, project } = fixture(t);
  const handlers = {}, appEvents = {}, windows = [], backends = [], errors = [];
  let setup, active = false, saves = 0, failStart = false, quits = 0;
  class Store {
    constructor() { this.data = { workspace: project, model: "deepseek-flash" }; this.ready = false; this.file = path.join(root, "settings.json"); }
    async load() { return this; } view() { return { ...this.data, keySource: "missing" }; }
    environment() { return { DEEPSEEK_API_KEY: key }; }
    async save(input) { saves++; this.data = { ...this.data, ...input }; }
  }
  class Backend {
    constructor() { backends.push(this); }
    async start(workspace) {
      if (failStart) throw Error("startup failed before spawning");
      return this.info = { url: `http://127.0.0.1:${18000 + backends.length}`, token: "offline-token", workspace };
    }
    async stop() { this.exited = true; return { forced: false }; }
  }
  class Window {
    constructor() { windows.push(this); this.webContents = { on() {}, setWindowOpenHandler() {},
      session: { setPermissionRequestHandler() {}, setPermissionCheckHandler() {} } }; }
    setMenu() {} on() {} once() {} show() {} focus() {} isDestroyed() { return false; } setEnabled() {} setTitle() {}
    loadURL(url) { this.webContents.mainFrame = { url }; return Promise.resolve(); }
  }
  const electron = { app: { requestSingleInstanceLock: () => true, getPath: () => root,
    whenReady: () => Promise.resolve(), on(name, callback) { appEvents[name] = callback; }, quit() { quits++; } },
    BrowserWindow: Window, ipcMain: { handle(name, callback) { handlers[name] = callback; }, on() {} },
    dialog: { showErrorBox(...args) { errors.push(args); } } };
  const context = { module: { exports: {} }, AbortSignal, __dirname: path.resolve(__dirname, "../desktop"),
    process: { argv: [], versions: {}, platform: process.platform },
    fetch: async () => Response.json(active ? [{ finished: false }] : []),
    require(name) {
      if (name === "./backend.cjs") return { Backend };
      if (name === "./settings.cjs") return { Settings: Store };
      if (name === "./settings-window.cjs") return { createSettingsWindow(_electron, options) {
        setup = options; return { win: new Window(), loaded: Promise.resolve() };
      } };
      return require(name);
    }
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../desktop/main.cjs"), "utf8"), context);
  context.module.exports.startDesktop(electron);
  await new Promise(setImmediate);
  assert.equal(backends.length, 0); assert.ok(setup);
  await setup.save({ workspace: project, model: "deepseek-flash", apiKey: key });
  assert.equal(backends.length, 1); assert.equal(saves, 1);
  active = true;
  await assert.rejects(setup.save({ workspace: project, model: "changed-model", apiKey: key }), /尚未保存完成/);
  assert.equal(saves, 1); assert.equal(backends.length, 1); assert.equal(backends[0].exited, undefined);
  const workbench = windows.at(-1), event = { sender: workbench.webContents, senderFrame: workbench.webContents.mainFrame };
  await assert.rejects(handlers["desktop:settings"](event), /尚未保存完成/);
  active = false; failStart = true;
  await assert.rejects(setup.save({ workspace: project, model: "deepseek-flash", apiKey: key }), /startup failed/);
  assert.equal(backends[0].exited, true);
  setup.closed(); assert.equal(quits, 1); // no stale workbench after a failed restart
  appEvents["before-quit"]({ preventDefault() {} });
  await new Promise(setImmediate);
  assert.equal(backends[0].exited, true); assert.deepEqual(errors, []);
});
