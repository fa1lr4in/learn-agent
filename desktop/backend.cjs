const fs = require("node:fs");
const path = require("node:path");
const { spawn, execFile } = require("node:child_process");
const { randomUUID } = require("node:crypto");
const { setTimeout: delay } = require("node:timers/promises");

function sameDirectory(first, second) {
  return process.platform === "win32" ? first.toLowerCase() === second.toLowerCase() : first === second;
}

function validateInfo(info, workspace, pid, instanceId) {
  const url = new URL(info.url);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.origin !== info.url ||
      !url.port || !/^[A-Za-z0-9_-]{20,}$/.test(info.token) || !Number.isSafeInteger(info.pid) || info.pid <= 0 ||
      (info.pid !== pid && info.parent_pid !== pid) ||
      info.instance_id !== instanceId || !sameDirectory(info.workspace, workspace)) {
    throw new Error("后端启动信息与本次实例不一致。");
  }
  return info;
}

async function checkBackend(info, fetcher = fetch) {
  const response = await fetcher(`${info.url}/instance`, {
    headers: { Authorization: `Bearer ${info.token}` }, redirect: "error", signal: AbortSignal.timeout(1000)
  });
  const identity = response.ok ? await response.json() : {};
  if (identity.instance_id !== info.instance_id || identity.pid !== info.pid || identity.api_version !== 1 ||
      !sameDirectory(identity.workspace || "", info.workspace)) throw new Error("后端未就绪或实例身份不一致。");
}

class Backend {
  constructor({ root = path.join(__dirname, ".."), python, logDirectory = path.join(root, "logs"),
                env = process.env, startupMs = 20000, shutdownMs = 15000,
                onUnexpectedExit = () => {} } = {}) {
    this.root = root;
    this.python = python || path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
    this.env = { ...env };
    for (const name of ["PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE"]) delete this.env[name];
    this.logDirectory = logDirectory;
    this.startupMs = startupMs;
    this.shutdownMs = shutdownMs;
    this.onUnexpectedExit = onUnexpectedExit;
    this.record = path.join(logDirectory, ".desktop-server.json");
    this.diagnostics = path.join(logDirectory, ".desktop-error.log");
    this.stderr = "";
    this.stopping = null;
  }

  diagnose(message) {
    let safe = `${new Date().toISOString()} ${message}\n${this.stderr}`;
    for (const secret of [this.env.DEEPSEEK_API_KEY, this.info?.token]) {
      if (secret) safe = safe.split(secret).join("[redacted]");
    }
    fs.mkdirSync(path.dirname(this.diagnostics), { recursive: true });
    fs.writeFileSync(this.diagnostics, safe, { mode: 0o600 });
  }

  async start(workspace) {
    if (this.child) throw new Error("此后端实例已启动。");
    const project = fs.realpathSync.native(workspace);
    if (!fs.statSync(project).isDirectory()) throw new Error("项目目录不存在或不是目录。");
    if (!fs.existsSync(this.python)) throw new Error("找不到 Python 运行时；源码版请创建 .venv，安装版请重新安装应用。");
    const instanceId = randomUUID();
    const child = spawn(this.python, ["-E", "-s", "-B", "-X", "utf8", path.join(this.root, "api.py"),
      "--workspace", project, "--log-dir", this.logDirectory, "--port", "0", "--managed", "--instance-id", instanceId], {
      cwd: this.root, env: this.env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
      detached: process.platform !== "win32"
    });
    this.child = child;
    child.stdin.on("error", () => {}); // the child can exit before EOF is sent
    this.exited = false;
    this.closed = new Promise(resolve => {
      child.once("error", () => { this.exited = true; resolve(); });
      child.once("exit", (code, signal) => {
        this.exited = true; resolve();
        if (this.info && !this.stopping) {
          this.removeRecord();
          this.diagnose(`后端意外退出（code=${code}, signal=${signal}），任务未自动重放。`);
          this.onUnexpectedExit();
        }
      });
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", text => { this.stderr = (this.stderr + text).slice(-12000); });
    let buffer = "", announced;
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", text => {
      buffer += text;
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).trim(); buffer = buffer.slice(newline + 1);
        if (line.startsWith("LEARN_AGENT_READY ") && announced === undefined) {
          try { announced = validateInfo(JSON.parse(line.slice(18)), project, child.pid, instanceId); }
          catch { announced = false; }
        }
      }
      buffer = buffer.slice(-16000);
    });
    try {
      const deadline = Date.now() + this.startupMs;
      while (Date.now() < deadline) {
        if (this.exited || announced === false) throw new Error("Python 后端启动失败。");
        if (announced) {
          try { await checkBackend(announced); break; } catch { /* bind and application startup are separate */ }
        }
        await delay(50);
      }
      if (!announced || this.exited || Date.now() >= deadline) throw new Error("等待 Python 后端就绪超时。");
      this.info = announced;
      fs.mkdirSync(path.dirname(this.record), { recursive: true });
      fs.writeFileSync(this.record, JSON.stringify(announced), { mode: 0o600 });
      return announced;
    } catch (error) {
      await this.stop();
      this.diagnose(error.message);
      throw new Error(`${error.message} 请在设置中检查模型密钥，并查看日志目录中的 .desktop-error.log。`);
    }
  }

  removeRecord() {
    if (!this.info) return;
    try {
      if (JSON.parse(fs.readFileSync(this.record, "utf8")).instance_id === this.info.instance_id) fs.unlinkSync(this.record);
    } catch (error) { if (error.code !== "ENOENT") this.diagnose("无法移除旧启动记录；下次启动不会复用它。"); }
  }

  stop() {
    if (!this.stopping) this.stopping = this.shutdown();
    return this.stopping;
  }

  async shutdown() {
    if (!this.child) return { forced: false };
    this.child.stdin.end();
    let timer;
    await Promise.race([this.closed, new Promise(resolve => { timer = setTimeout(resolve, this.shutdownMs); })]);
    clearTimeout(timer);
    const forced = !this.exited;
    if (forced) {
      this.diagnose("后端收尾超时，强制结束本应用启动的进程树；当前会话可能未保存完整。");
      if (process.platform === "win32") {
        await new Promise(resolve => execFile(path.join(this.env.SystemRoot || "C:\\Windows", "System32", "taskkill.exe"),
          ["/PID", String(this.child.pid), "/T", "/F"], { windowsHide: true, timeout: 5000,
            env: { SystemRoot: this.env.SystemRoot || "C:\\Windows" } }, resolve));
      } else {
        try { process.kill(-this.child.pid, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") throw error; }
      }
      if (!this.exited) this.child.kill("SIGKILL");
      await Promise.race([this.closed, new Promise(resolve => { timer = setTimeout(resolve, 5000); })]);
      clearTimeout(timer);
      if (!this.exited) throw new Error("无法确认后端进程退出，请查看诊断日志。");
    }
    this.removeRecord();
    return { forced };
  }
}

module.exports = { Backend, checkBackend, sameDirectory, validateInfo };
