const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { chooseProject, createWindow } = require("../desktop/main.cjs");
const { checkBackend, validateInfo } = require("../desktop/backend.cjs");

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "learn-agent-desktop-test-"));
  t.after(() => {
    assert.ok(path.resolve(root).startsWith(path.resolve(os.tmpdir()) + path.sep));
    fs.rmSync(root, { recursive: true, force: true });
  });
  const project = path.join(root, "中文 project");
  fs.mkdirSync(project);
  const file = path.join(root, "server.json");
  const info = { url: "http://127.0.0.1:8000", token: "offline-local-token-for-tests", workspace: project,
    pid: 123, instance_id: "own-instance" };
  fs.writeFileSync(file, JSON.stringify(info));
  const frame = { url: info.url + "/" };
  const win = { webContents: { mainFrame: frame } };
  const event = { sender: win.webContents, senderFrame: frame };
  return { root, project, file, info, win, event };
}

test("desktop accepts only its child process announcement", t => {
  const { info } = fixture(t);
  assert.deepEqual(validateInfo(info, info.workspace, 123, "own-instance"), info);
});

test("service records cannot send the token to another URL or use relative projects", t => {
  const { file, info } = fixture(t);
  for (const changed of [
    { url: "https://other.example" }, { url: "http://127.0.0.1:8000@other.example" },
    { url: "http://127.0.0.1:8000/other" }, { workspace: "../project" }, { token: "bad\nheader" }
  ]) {
    assert.throws(() => validateInfo({ ...info, ...changed }, info.workspace, 123, "own-instance"));
  }
});

test("a different PID, instance or workspace is rejected", t => {
  const { info } = fixture(t);
  for (const changed of [{ pid: 456 }, { instance_id: "foreign" }, { workspace: "other" }]) {
    assert.throws(() => validateInfo({ ...info, ...changed }, info.workspace, 123, "own-instance"));
  }
});

test("desktop checks the existing service with local authorization and no redirects", async t => {
  const { info } = fixture(t);
  await checkBackend(info, async (url, options) => {
    assert.equal(url, "http://127.0.0.1:8000/instance");
    assert.equal(options.headers.Authorization, `Bearer ${info.token}`);
    assert.equal(options.redirect, "error");
    assert.ok(options.signal instanceof AbortSignal);
    return Response.json({ ...info, api_version: 1 });
  });
});

test("stale authorization and invalid API replies do not open the workbench", async t => {
  const { info } = fixture(t);
  for (const response of [new Response("{}", { status: 401 }), new Response("{}"), new Response("not-json")]) {
    await assert.rejects(checkBackend(info, async () => response));
  }
});

test("network failure does not submit or start a backend", async t => {
  const { info } = fixture(t);
  let calls = 0;
  await assert.rejects(checkBackend(info, async () => { calls++; throw new Error("offline"); }));
  assert.equal(calls, 1);
});

test("choosing the current directory returns its path without changing the service", async t => {
  const { event, win, info, file } = fixture(t);
  const original = fs.readFileSync(file, "utf8");
  const dialog = { async showOpenDialog(parent, options) {
    assert.equal(parent, win);
    assert.deepEqual(options.properties, ["openDirectory"]);
    assert.equal(options.defaultPath, info.workspace);
    return { canceled: false, filePaths: [info.workspace] };
  } };
  assert.deepEqual(await chooseProject(event, win, info, dialog), { path: fs.realpathSync(info.workspace), current: true });
  assert.equal(fs.readFileSync(file, "utf8"), original);
});

test("choosing a different directory waits for the owned backend switch", async t => {
  const { root, event, win, info } = fixture(t);
  const other = path.join(root, "other"); fs.mkdirSync(other);
  let switched;
  const result = await chooseProject(event, win, info, { showOpenDialog: async () => ({ canceled: false, filePaths: [other] }) }, [],
    async chosen => { switched = chosen; });
  assert.deepEqual(result, { path: fs.realpathSync(other), current: false });
  assert.notEqual(info.workspace, result.path);
  assert.equal(switched, result.path);
});

test("cancelled native dialogs return no selected project", async t => {
  const { event, win, info } = fixture(t);
  assert.equal(await chooseProject(event, win, info, { showOpenDialog: async () => ({ canceled: true, filePaths: [] }) }), null);
});

test("foreign windows, subframes, pages and supplied arguments cannot invoke the picker", async t => {
  const { event, win, info } = fixture(t);
  const dialog = { showOpenDialog() { assert.fail("unauthorized dialog"); } };
  for (const invalid of [{ ...event, sender: {} }, { ...event, senderFrame: { url: info.url + "/" } }, { ...event, senderFrame: null }]) {
    await assert.rejects(chooseProject(invalid, win, info, dialog));
  }
  event.senderFrame.url = "https://other.example/";
  await assert.rejects(chooseProject(event, win, info, dialog));
  event.senderFrame.url = info.url + "/";
  await assert.rejects(chooseProject(event, win, info, dialog, ["arbitrary-path"]));
});

test("the picker does not accept a selected file as a project", async t => {
  const { file, event, win, info } = fixture(t);
  await assert.rejects(chooseProject(event, win, info, { showOpenDialog: async () => ({ canceled: false, filePaths: [file] }) }));
});

test("the preload exposes only project selection and typed theme operations", async () => {
  let bridge, invoked;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../desktop/preload.cjs"), "utf8"), {
    require(name) {
      assert.equal(name, "electron");
      return { contextBridge: { exposeInMainWorld(name, api) { assert.equal(name, "learnAgentDesktop"); bridge = api; } },
        ipcRenderer: { sendSync(name) { assert.equal(name, "theme:get"); return "dark"; },
          invoke(...args) { invoked = args; return Promise.resolve(null); } } };
    }
  });
  assert.deepEqual(Object.keys(bridge), ["chooseProject", "openSettings", "getTheme", "setTheme"]);
  assert.equal(await bridge.chooseProject("run-command", "anything"), null);
  assert.deepEqual(invoked, ["project:choose"]);
  await bridge.openSettings("ignored"); assert.deepEqual(invoked, ["desktop:settings"]);
  assert.equal(bridge.getTheme(), "dark");
  await bridge.setTheme("light"); assert.deepEqual(invoked, ["theme:set", "light"]);
});

test("Electron starts the entry even when its loader does not set require.main", () => {
  const events = {};
  let boot;
  const electron = { app: {
    requestSingleInstanceLock() { return true; },
    on(name, callback) { events[name] = callback; },
    whenReady() { return { then(callback) { boot = callback; return { catch() {} }; } }; }
  }, dialog: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../desktop/main.cjs"), "utf8"), {
    require(name) { return name === "electron" ? electron : name.startsWith("./") ? require("../desktop/" + name.slice(2)) : require(name); },
    process: { versions: { electron: "45.0.0" } },
    module: { exports: {} }, __dirname: path.join(__dirname, "../desktop")
  });
  assert.equal(typeof boot, "function");
  assert.equal(typeof events["window-all-closed"], "function");
});

test("window isolates the renderer and blocks navigation, popups and web permissions", async t => {
  const { info } = fixture(t);
  const events = {}, handlers = {}, permissions = {}, listeners = {};
  let options, opened, loaded, removed;
  class Window {
    constructor(settings) {
      options = settings;
      this.webContents = { on(name, callback) { events[name] = callback; },
        setWindowOpenHandler(callback) { opened = callback; },
        session: { setPermissionRequestHandler(callback) { permissions.request = callback; },
          setPermissionCheckHandler(callback) { permissions.check = callback; } } };
    }
    setMenu(value) { assert.equal(value, null); }
    on(name, callback) { events[name] = callback; }
    once(name, callback) { events[name] = callback; }
    loadURL(url) { loaded = url; return Promise.resolve(); }
    show() {}
  }
  const result = createWindow(info, { BrowserWindow: Window, dialog: {}, ipcMain: {
    handle(name, callback) { handlers[name] = callback; }, removeHandler(name) { removed = name; },
    on(name, callback) { listeners[name] = callback; }, removeAllListeners(name) { delete listeners[name]; }
  } }, async () => {}, { value: "dark", save(value) { this.value = value; } }, () => {});
  await result.loaded;
  assert.equal(loaded, info.url + "/");
  assert.equal(options.webPreferences.nodeIntegration, false);
  assert.equal(options.webPreferences.contextIsolation, true);
  assert.equal(options.webPreferences.sandbox, true);
  assert.equal(options.webPreferences.webSecurity, true);
  assert.equal(options.webPreferences.webviewTag, false);
  assert.ok(path.isAbsolute(options.webPreferences.preload));
  for (const name of ["will-navigate", "will-redirect"]) {
    let blocked = false;
    events[name]({ preventDefault() { blocked = true; } }, "https://other.example/");
    assert.equal(blocked, true);
    events[name]({ preventDefault() { assert.fail("blocked own workbench"); } }, info.url + "/");
  }
  assert.deepEqual(opened(), { action: "deny" });
  permissions.request(null, "camera", allowed => assert.equal(allowed, false));
  assert.equal(permissions.check(), false);
  assert.deepEqual(Object.keys(handlers), ["project:choose", "theme:set", "desktop:settings"]);
  const event = { sender: result.win.webContents, senderFrame: { url: info.url + "/" } };
  result.win.webContents.mainFrame = event.senderFrame;
  listeners["theme:get"](event); assert.equal(event.returnValue, "dark");
  assert.throws(() => handlers["theme:set"](event, "invalid"));
  assert.throws(() => handlers["theme:set"]({ ...event, sender: {} }, "light"));
  const foreign = { ...event, sender: {} }; listeners["theme:get"](foreign); assert.equal(foreign.returnValue, null);
  events.closed(); assert.equal(removed, "desktop:settings"); assert.deepEqual(listeners, {});
});
