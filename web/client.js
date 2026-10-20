// HTTP 请求与 SSE 解码独立于页面，POST 失败时不自动重试。
export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export async function* parseEvents(body) {
  const reader = body.getReader(), decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let separator;
      while ((separator = /\r?\n\r?\n/.exec(buffer))) {
        const raw = buffer.slice(0, separator.index);
        buffer = buffer.slice(separator.index + separator[0].length);
        const fields = {}, data = [];
        for (const line of raw.split(/\r?\n/)) {
          if (line.startsWith(":")) continue;
          const colon = line.indexOf(":");
          const key = colon < 0 ? line : line.slice(0, colon);
          const text = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
          if (key === "data") data.push(text);
          else if (key === "id" || key === "event") fields[key] = text;
        }
        if (data.length) {
          const frame = JSON.parse(data.join("\n"));
          if (!frame || frame.id !== fields.id || frame.event !== fields.event) throw new Error("事件内容与序号不一致，请重新连接。");
          yield frame;
        }
      }
      if (done) return; // 半截帧不会推进游标。
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

export function sequence(cursor, taskId) {
  const parts = cursor.split(":");
  if (parts.length !== 2 || parts[0] !== taskId || !/^\d{1,12}$/.test(parts[1])) throw new Error("任务游标无效，请重新连接。");
  return Number(parts[1]);
}

export class ApiClient {
  constructor(token, fetcher = (...args) => fetch(...args)) { this.token = token; this.fetcher = fetcher; }
  async request(path, body) {
    const response = await this.fetcher(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { Authorization: `Bearer ${this.token}`, ...(body === undefined ? {} : { "Content-Type": "application/json; charset=utf-8" }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), cache: "no-store",
    });
    const data = await response.json();
    if (!response.ok) throw new ApiError(response.status === 401 ? "本地连接已失效，请刷新页面。" :
      (typeof data.detail === "string" ? data.detail : `请求失败（${response.status}）`), response.status);
    return data;
  }
  async watch(taskId, cursor, onEvent, signal) {
    let last = cursor === null ? -1 : sequence(cursor, taskId);
    const response = await this.fetcher(`/tasks/${taskId}/events`, {
      headers: { Authorization: `Bearer ${this.token}`, Accept: "text/event-stream", ...(cursor ? { "Last-Event-ID": cursor } : {}) },
      signal, cache: "no-store",
    });
    if (!response.ok) throw new ApiError(`事件连接失败（${response.status}），请重新连接。`, response.status);
    if (!response.headers.get("content-type")?.startsWith("text/event-stream") || !response.body) throw new Error("服务没有返回事件流。");
    for await (const frame of parseEvents(response.body)) {
      if (frame.task_id !== taskId) throw new Error("收到其他任务的事件，已停止观察。");
      const current = sequence(frame.id, taskId);
      if (current <= last) continue;
      if (frame.event !== "snapshot" && current !== last + 1) throw new Error("事件序号不连续，请重新连接。");
      await onEvent(frame);
      cursor = frame.id;
      last = current;
    }
    return cursor;
  }
}
