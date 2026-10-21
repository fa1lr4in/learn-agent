import test from "node:test";
import assert from "node:assert/strict";
import { ApiClient, parseEvents, sequence } from "../web/client.js";
import { historyItems, sessionPhase, taskItems } from "../web/view-model.js";

const frame = (n, event = "note", data = {}, task = "task") => ({ id: `${task}:${n}`, event, data, task_id: task });
const wire = f => `id: ${f.id}\r\nevent: ${f.event}\r\ndata: ${JSON.stringify(f)}\r\n\r\n`;
const body = bytes => new ReadableStream({ start(c) { for (const b of bytes) c.enqueue(b); c.close(); } });
const response = (...frames) => new Response(wireBytes(frames), { headers: { "Content-Type": "text/event-stream" } });
const wireBytes = frames => new TextEncoder().encode(frames.map(wire).join(""));
const collect = async iterable => { const values = []; for await (const value of iterable) values.push(value); return values; };

test("SSE supports split UTF-8, CRLF and keepalive comments", async () => {
  const bytes = new TextEncoder().encode(": keepalive\r\n\r\n" + wire(frame(1, "note", { text: "中文" })));
  const frames = await collect(parseEvents(body([...bytes].map(b => new Uint8Array([b])))));
  assert.equal(frames.length, 1); assert.equal(frames[0].data.text, "中文");
});
test("SSE supports multiline JSON and ignores unfinished tails", async () => {
  const raw = 'id: task:1\nevent: note\ndata: {"id":"task:1",\ndata: "event":"note"}\n\n' + wire(frame(2)).slice(0, -2);
  assert.equal((await collect(parseEvents(body([new TextEncoder().encode(raw)])))).length, 1);
});
test("SSE validates the wire ID against JSON data", async () => {
  await assert.rejects(collect(parseEvents(body([new TextEncoder().encode(wire(frame(1)).replace("id: task:1", "id: other:1"))]))), /不一致/);
});
test("cursor validates task ownership and bounded numeric sequence", () => {
  assert.equal(sequence("task:0", "task"), 0);
  for (const id of ["other:1", "task:-1", "task:１", "task:1:2", "task:1234567890123"]) assert.throws(() => sequence(id, "task"));
});
test("requests use authorization headers and UTF-8 JSON without retrying POST", async () => {
  let requests = 0;
  const client = new ApiClient("local-token", async (path, options) => {
    requests++; assert.equal(path, "/sessions/x/tasks"); assert.equal(options.headers.Authorization, "Bearer local-token");
    assert.deepEqual(JSON.parse(options.body), { prompt: "中文" }); throw new Error("connection lost");
  });
  await assert.rejects(client.request("/sessions/x/tasks", { prompt: "中文" })); assert.equal(requests, 1);
});
test("HTTP errors retain status for stale approvals and authentication", async () => {
  const client = new ApiClient("token", async () => new Response(JSON.stringify({ detail: "审批失效" }), { status: 409 }));
  await assert.rejects(client.request("/approvals", {}), error => error.status === 409 && error.message === "审批失效");
});
test("default fetch is not invoked with ApiClient as the native receiver", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = function () {
    assert.ok(this === undefined || this === globalThis);
    return Promise.resolve(new Response('{}'));
  };
  try { await new ApiClient("token").request("/sessions"); }
  finally { globalThis.fetch = original; }
});
test("watch resumes by header, skips duplicates and delivers each later event once", async () => {
  const seen = [];
  const client = new ApiClient("token", async (path, options) => {
    assert.equal(options.headers["Last-Event-ID"], "task:3");
    return response(frame(3), frame(4), frame(4), frame(5, "task_finished"));
  });
  assert.equal(await client.watch("task", "task:3", f => seen.push(f.id)), "task:5");
  assert.deepEqual(seen, ["task:4", "task:5"]);
});
test("watch rejects gaps and foreign tasks before delivery", async () => {
  for (const bad of [frame(3), frame(2, "note", {}, "other")]) {
    const seen = [], client = new ApiClient("token", async () => response(bad));
    await assert.rejects(client.watch("task", "task:1", f => seen.push(f)));
    assert.equal(seen.length, 0);
  }
});
test("first snapshot may cover a longer prefix of the event journal", async () => {
  const client = new ApiClient("token", async () => response(frame(50, "snapshot"), frame(51)));
  assert.equal(await client.watch("task", null, () => {}), "task:51");
});
test("empty completed stream does not create another task", async () => {
  const requests = [], client = new ApiClient("token", async (path, options) => {
    requests.push({ path, options }); return response();
  });
  assert.equal(await client.watch("task", "task:9", () => {}), "task:9");
  assert.equal(requests.length, 1); assert.equal(requests[0].options.method, undefined);
});
test("wrong stream type and HTTP failures are rejected", async () => {
  for (const result of [new Response("{}"), new Response("no", { status: 401 })]) {
    await assert.rejects(new ApiClient("token", async () => result).watch("task", null, () => {}));
  }
});
test("observer forwards abort signal", async () => {
  const controller = new AbortController(); controller.abort();
  const client = new ApiClient("token", async (path, options) => { assert.equal(options.signal, controller.signal); throw new DOMException("aborted", "AbortError"); });
  await assert.rejects(client.watch("task", null, () => {}, controller.signal), { name: "AbortError" });
});

const call = (id = "call") => ({ id, type: "function", function: { name: "edit_file", arguments: JSON.stringify({ path: "main.py" }) } });
const reply = (content, calls = []) => ({ choices: [{ message: { role: "assistant", content, tool_calls: calls } }] });
test("streamed text is replaced by full reply without duplicated content", () => {
  const items = taskItems([{ event: "model_delta", step: 1, delta: { content: "你好" } }, { event: "model_response", step: 1, response: reply("你好！") }]);
  assert.equal(items.length, 1); assert.equal(items[0].content, "你好！");
});
test("only the current user prompt is added from initial history", () => {
  const items = taskItems([{ event: "run_start", initial_messages: [{ role: "system", content: "rules" }, { role: "user", content: "old" }, { role: "user", content: "new" }] }]);
  assert.deepEqual(items.map(i => i.content), ["new"]);
});
test("tool requests and approvals never claim execution success", () => {
  const events = [{ event: "model_response", step: 1, response: reply(null, [call()]) }];
  assert.equal(taskItems(events)[0].status, "queued");
  events.push({ event: "tool_waiting", step: 1, tool_call_id: "call", tool: "edit_file", preview: "-old\n+new" });
  assert.equal(taskItems(events)[0].status, "waiting_approval"); assert.equal(taskItems(events)[0].preview, "-old\n+new");
  events.push({ event: "tool_approval", step: 1, tool_call_id: "call", approved: true });
  assert.equal(taskItems(events)[0].status, "queued");
});
test("failed result remains failed and rejection remains denied", () => {
  const base = [{ event: "model_response", step: 1, response: reply(null, [call()]) }];
  assert.equal(taskItems([...base, { event: "tool_result", step: 1, tool_call_id: "call", result: { ok: false } }])[0].status, "failed");
  assert.equal(taskItems([...base, { event: "tool_approval", step: 1, tool_call_id: "call", approved: false }, { event: "run_end" }])[0].status, "denied");
});
test("interrupted execution is unknown while unstarted requests are not executed", () => {
  const events = [{ event: "model_response", step: 1, response: reply(null, [call("one"), call("two")]) },
    { event: "tool_started", step: 1, tool_call_id: "one" }, { event: "run_end" }];
  assert.deepEqual(taskItems(events).map(i => i.status), ["unknown", "cancelled"]);
});
test("reused tool IDs in different model steps remain independent", () => {
  const events = [1, 2].flatMap(step => [{ event: "model_response", step, response: reply(null, [call()]) }, { event: "tool_result", step, tool_call_id: "call", result: { ok: true, content: String(step) } }]);
  assert.deepEqual(taskItems(events).map(i => i.result.content), ["1", "2"]);
});
test("history uses actual tool result messages and handles repeated call IDs", () => {
  const messages = [1, 2].flatMap(n => [{ role: "assistant", tool_calls: [call()] }, { role: "tool", tool_call_id: "call", content: JSON.stringify({ ok: true, stdout: String(n) }) }]);
  assert.deepEqual(historyItems(messages).map(i => i.result.stdout), ["1", "2"]);
});

test("saved approvals preserve denial and cancellation without inventing results", () => {
  const messages = ["denied", "cancelled", "approved"].map(id => ({ role: "assistant", tool_calls: [call(id)] }));
  const runs = [{ call_id: "denied", approved: false, approval_source: "user", result: null },
    { call_id: "cancelled", approved: null, result: null }, { call_id: "approved", approved: true, result: null }];
  const items = historyItems(messages, runs);
  assert.deepEqual(items.map(i => i.status), ["denied", "cancelled", "unknown"]);
  assert.equal(items[0].approvalSource, "user");
  assert.ok(items.every(i => !i.result));
});

test("saved tool records with reused IDs attach in transcript order", () => {
  const messages = [{ role: "assistant", tool_calls: [call()] }, { role: "tool", tool_call_id: "call", content: '{"ok":true,"stdout":"5050"}' },
    { role: "assistant", tool_calls: [call()] }, { role: "tool", tool_call_id: "call", content: '{"ok":false,"error":{"type":"NotExecuted"}}' }];
  const runs = [{ call_id: "call", approved: true, approval_source: "user", result: { ok: true, stdout: "5050" } },
    { call_id: "call", approved: false, approval_source: "user", result: null }];
  const items = historyItems(messages, runs);
  assert.deepEqual(items.map(i => i.status), ["completed", "denied"]);
  assert.equal(items[0].result.stdout, "5050");
  assert.equal(items[1].result.error.type, "NotExecuted");
});

test("archived tool requests without execution evidence are not queued for replay", () => {
  assert.equal(historyItems([{ role: "assistant", tool_calls: [call()] }])[0].status, "cancelled");
});

test("archived task phase reflects the saved end reason", () => {
  assert.equal(sessionPhase(null), "idle");
  assert.equal(sessionPhase("stop"), "completed");
  for (const reason of ["cancelled", "user_denied", "policy_denied"]) assert.equal(sessionPhase(reason), "cancelled");
  for (const reason of ["max_model_calls", "max_total_tokens", "usage_unavailable", "context_limit", "plan_incomplete"]) assert.equal(sessionPhase(reason), "paused");
  for (const reason of ["error", "abnormal_finish"]) assert.equal(sessionPhase(reason), "failed");
});
test("untrusted model and file content stay plain data", () => {
  const content = '<img src=x onerror="alert(1)">';
  assert.equal(taskItems([{ event: "model_response", step: 1, response: reply(content) }])[0].content, content);
});
test("invalid tool arguments remain visible data and do not crash the renderer", () => {
  for (const raw of ["null", "42", "[]", "{broken"]) {
    const invalid = call(); invalid.function.arguments = raw;
    const item = taskItems([{ event: "model_response", step: 1, response: reply(null, [invalid]) }])[0];
    assert.deepEqual(item.arguments, { raw }); assert.equal(item.status, "queued");
  }
});
