const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");

function projectDirectory(value) {
  if (typeof value !== "string" || !path.isAbsolute(value)) throw new Error("请选择已有的项目目录。");
  try {
    const resolved = fs.realpathSync.native(value);
    if (fs.statSync(resolved).isDirectory()) return resolved;
  } catch { /* report a fixed message, never echo submitted credentials */ }
  throw new Error("项目目录不存在或无法访问，请重新选择。");
}

function modelName(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(value.trim())) {
    throw new Error("模型名称只能包含字母、数字、点、下划线与连字符，最长 100 字符。");
  }
  return value.trim();
}

class Settings {
  #key = "";
  #env;

  constructor(directory, safeStorage, { workspace, env = process.env } = {}) {
    this.file = path.join(directory, "settings.json");
    this.crypto = safeStorage;
    this.#env = env;
    this.data = { version: 1, model: env.DEEPSEEK_MODEL || "deepseek-flash", workspace, encryptedKey: "" };
    this.source = "missing";
    this.warning = "";
    this.configured = false;
  }

  async load(workspaceOverride) {
    this.#key = (this.#env.DEEPSEEK_API_KEY || "").trim();
    this.source = this.#key ? "environment" : "missing";
    try {
      const saved = JSON.parse(fs.readFileSync(this.file, "utf8"));
      if (saved.version !== 1 || typeof saved.workspace !== "string" || typeof saved.encryptedKey !== "string" ||
          Object.keys(saved).some(key => !["version", "model", "workspace", "encryptedKey"].includes(key)) ||
          saved.encryptedKey.length > 16384 || !/^[A-Za-z0-9+/]*={0,2}$/.test(saved.encryptedKey)) throw new Error();
      modelName(saved.model);
      this.data = saved;
      if (saved.encryptedKey) {
        const decrypted = await this.crypto.decryptStringAsync(Buffer.from(saved.encryptedKey, "base64"));
        if (!decrypted.result) throw new Error();
        this.#key = decrypted.result;
        this.source = "saved";
      }
      this.configured = true;
    } catch (error) {
      if (error.code !== "ENOENT") this.warning = "本机配置无法读取或密钥无法解密，请检查后重新保存设置。";
    }
    if (workspaceOverride) this.data.workspace = workspaceOverride;
    try { this.data.workspace = projectDirectory(this.data.workspace); }
    catch (error) { this.warning = error.message; this.configured = false; }
    try { this.data.model = modelName(this.data.model); }
    catch { this.warning = "模型名称无效，请重新填写。"; this.configured = false; }
    return this;
  }

  get ready() { return this.configured && Boolean(this.#key) && !this.warning; }

  view() {
    return { model: this.data.model, workspace: this.data.workspace, keySource: this.source,
      configured: this.configured, warning: this.warning };
  }

  environment() {
    return { ...this.#env, DEEPSEEK_API_KEY: this.#key, DEEPSEEK_MODEL: this.data.model };
  }

  write(data) {
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const temporary = path.join(path.dirname(this.file), `.settings-${randomUUID()}.tmp`);
    try {
      fs.writeFileSync(temporary, JSON.stringify(data), { mode: 0o600 });
      fs.renameSync(temporary, this.file);
    } catch {
      throw new Error("无法保存本机设置，请检查用户数据目录的写入权限。");
    } finally {
      if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
    }
    this.data = data;
  }

  async save(input) {
    if (!input || typeof input !== "object" || Array.isArray(input) ||
        Object.keys(input).some(key => !["workspace", "model", "apiKey"].includes(key)) || typeof input.apiKey !== "string") {
      throw new Error("设置格式无效。");
    }
    const workspace = projectDirectory(input.workspace), model = modelName(input.model);
    const key = input.apiKey.trim() || this.#key;
    if (!key) throw new Error("请在此窗口输入 DeepSeek API Key，或先配置已有环境变量。");
    if (!/^[\x21-\x7e]{1,512}$/.test(key)) throw new Error("API Key 格式无效，请检查空格、换行与长度。");
    if (!await this.crypto.isAsyncEncryptionAvailable()) throw new Error("系统加密暂不可用，未保存密钥，请稍后重试。");
    let encrypted;
    try { encrypted = await this.crypto.encryptStringAsync(key); }
    catch { throw new Error("系统加密失败，未保存密钥，请稍后重试。"); }
    this.write({ version: 1, model, workspace, encryptedKey: encrypted.toString("base64") });
    this.#key = key;
    this.source = "saved";
    this.configured = true;
    this.warning = "";
    return this.view();
  }

  rememberProject(workspace) {
    this.write({ ...this.data, workspace: projectDirectory(workspace) });
  }

  redact(text) {
    let safe = text;
    for (const key of [this.#key, this.#env.DEEPSEEK_API_KEY]) if (key) safe = safe.split(key).join("[redacted]");
    return safe;
  }
}

module.exports = { Settings };
