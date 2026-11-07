import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it } from "vitest";
import WebSocket from "ws";
import { AGENT, pairDevice, setup } from "./helpers.js";

let cleanup: Array<() => Promise<unknown>> = [];
afterEach(async () => {
  for (const c of cleanup) await c();
  cleanup = [];
});

async function app(over = {}) {
  const b = await setup(over);
  cleanup.push(() => b.app.close());
  return b;
}
type App = Awaited<ReturnType<typeof app>>;

const flush = () => new Promise((r) => setImmediate(r));
const post = (b: App, url: string, headers: Record<string, string>, payload?: object) =>
  b.app.inject({ method: "POST", url, headers, ...(payload ? { payload } : {}) });

describe("call authorization", () => {
  it("POST /end treats a phone outside a targeted call like GET: 404, nothing disclosed", async () => {
    const b = await app();
    const a = await pairDevice(b, "A");
    const other = await pairDevice(b, "B");
    const { call } = (await post(b, "/v1/calls", AGENT, { reason: "private", device_ids: [a.device_id] })).json();

    const url = `/v1/calls/${call.call_id}/end`;
    expect((await post(b, url, other.headers)).statusCode).toBe(404);
    await post(b, `/v1/calls/${call.call_id}/decline`, a.headers);
    const r = await post(b, url, other.headers);
    expect(r.statusCode).toBe(404);
    expect(r.body).not.toContain("private");
    expect((await post(b, url, a.headers)).statusCode).toBe(200);
  });

  it("a phone can't hang up another phone's call to the agent", async () => {
    const b = await app();
    const a = await pairDevice(b, "A");
    const other = await pairDevice(b, "B");
    b.ctx.hub.isAgentOnline = () => true;

    const { call } = (await post(b, "/v1/calls", a.headers, {})).json();
    const url = `/v1/calls/${call.call_id}/end`;
    expect((await post(b, url, other.headers)).statusCode).toBe(403);
    await post(b, `/v1/calls/${call.call_id}/accept`, AGENT);
    expect((await post(b, url, other.headers)).statusCode).toBe(403);
    expect(b.ctx.calls!.get(call.call_id)!.state).toBe("active");
    expect((await post(b, url, a.headers)).json().state).toBe("ended");
  });

  it("revoking a phone on a call ends the call and closes the room", async () => {
    const b = await app();
    const a = await pairDevice(b, "A");
    const other = await pairDevice(b, "B");
    const { call } = (await post(b, "/v1/calls", AGENT, { reason: "x" })).json();
    await post(b, `/v1/calls/${call.call_id}/accept`, a.headers);
    await post(b, `/v1/calls/${call.call_id}/accept`, other.headers);

    await b.app.inject({ method: "DELETE", url: `/v1/devices/${a.device_id}`, headers: AGENT });
    await flush();
    expect(b.ctx.calls!.get(call.call_id)!.state).toBe("ended");
    expect(b.livekit.deleted).toContain(call.call_id);
  });

  it("revoking the only ringing phone cancels the call; another phone keeps it ringing", async () => {
    const b = await app();
    const a = await pairDevice(b, "A");
    const other = await pairDevice(b, "B");
    const { call } = (await post(b, "/v1/calls", AGENT, { reason: "x" })).json();

    await b.app.inject({ method: "DELETE", url: `/v1/devices/${a.device_id}`, headers: AGENT });
    expect(b.ctx.calls!.get(call.call_id)!.state).toBe("ringing");
    await b.app.inject({ method: "DELETE", url: `/v1/devices/${other.device_id}`, headers: AGENT });
    expect(b.ctx.calls!.get(call.call_id)!.state).toBe("canceled");
  });

  it("a failing ring timer is logged, not fatal, and the call still ends once the database recovers", async () => {
    const b = await app({ ringTimeoutS: 0.05 });
    await pairDevice(b);
    const { call } = (await post(b, "/v1/calls", AGENT, { reason: "x" })).json();
    b.ctx.db.exec("CREATE TRIGGER no_events BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'disk full'); END");
    await new Promise((r) => setTimeout(r, 100));
    b.ctx.db.exec("DROP TRIGGER no_events");
    expect(b.ctx.calls!.get(call.call_id)).toBeDefined();
  });
});

describe("event log", () => {
  it("a failed event write leaves no row behind", async () => {
    const b = await app();
    b.ctx.db.exec("CREATE TRIGGER no_body BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'disk full'); END");
    const before = b.ctx.events.lastSeq();
    expect(() => b.ctx.events.emit({ type: "msg.read", msg_id: "m", by: "user" })).toThrow();
    expect(b.ctx.events.lastSeq()).toBe(before);
  });

  it("a bad stored row doesn't block the cursor", async () => {
    const b = await app();
    b.ctx.db.prepare("INSERT INTO events (id, type, ts, body) VALUES ('evt_x', 'msg.new', 1, '')").run();
    await post(b, "/v1/messages", AGENT, { text: "after" });
    const page = b.ctx.events.list(0, 500);
    expect(page.events.map((e) => e.type)).toEqual(["msg.new"]);
    expect(page.nextAfterSeq).toBe(b.ctx.events.lastSeq());
    expect((await b.app.inject({ url: "/v1/events?after_seq=0", headers: AGENT })).statusCode).toBe(200);
  });

  it("lone surrogates never reach the log", async () => {
    const b = await app();
    const payload = JSON.stringify({ text: "bad \ud800 ok 😀", reply_to: "msg_\ud83d" });
    const r = await b.app.inject({ method: "POST", url: "/v1/messages", headers: { ...AGENT, "content-type": "application/json" }, payload });
    expect(r.statusCode).toBe(201);
    const [e] = b.ctx.events.list(0, 500).events as Array<{ text: string; reply_to: string }>;
    expect(e!.text).toBe("bad � ok 😀");
    expect(e!.reply_to).toBe("msg_�");
  });
});

describe("pairing limiter", () => {
  const pair = (b: App, xff: string) =>
    b.app.inject({
      method: "POST",
      url: "/v1/pair",
      headers: { "x-forwarded-for": xff },
      payload: { pairing_token: "pt_nope", device_name: "x", platform: "android" },
    });

  it("reads the address the proxy added, not the part the client sent", async () => {
    const b = await app();
    for (let i = 0; i < 10; i++) expect((await pair(b, `10.0.0.${i}, 203.0.113.7`)).statusCode).toBe(401);
    expect((await pair(b, "10.9.9.9, 203.0.113.7")).statusCode).toBe(429);
    expect((await pair(b, "10.9.9.9, 203.0.113.8")).statusCode).toBe(401);
  });

  it("ignores X-Forwarded-For with no proxy configured", async () => {
    const b = await app({ trustedProxies: 0 });
    for (let i = 0; i < 10; i++) expect((await pair(b, `203.0.113.${i}`)).statusCode).toBe(401);
    expect((await pair(b, "203.0.113.200")).statusCode).toBe(429);
  });
});

describe("websocket", () => {
  async function listen(b: App) {
    await b.app.listen({ host: "127.0.0.1", port: 0 });
    return `ws://127.0.0.1:${(b.app.server.address() as AddressInfo).port}/v1/ws`;
  }

  /** Collects events until `done` says stop. */
  function collect(url: string, token: string, done: (seen: Array<{ type: string; seq?: number; text?: string }>) => boolean) {
    return new Promise<Array<{ type: string; seq?: number; text?: string }>>((resolve, reject) => {
      const ws = new WebSocket(url, { headers: { authorization: `Bearer ${token}` } });
      const seen: Array<{ type: string; seq?: number; text?: string }> = [];
      ws.on("message", (d) => {
        seen.push(JSON.parse(String(d)));
        if (done(seen)) {
          ws.close();
          resolve(seen);
        }
      });
      ws.on("error", reject);
    });
  }

  it("replays a long history in order, once, with live events after it", async () => {
    const b = await app();
    const dev = await pairDevice(b);
    for (let i = 0; i < 450; i++) b.ctx.events.emit({ type: "msg.read", msg_id: `m${i}`, by: "agent" });
    const url = await listen(b);
    const last = b.ctx.events.lastSeq();

    const got = collect(url, dev.device_jwt, (s) => s.some((e) => e.type === "msg.new"));
    await new Promise((r) => setTimeout(r, 1));
    await post(b, "/v1/messages", AGENT, { text: "live" });
    const seen = await got;

    expect(seen[0]!.type).toBe("ws.hello");
    const seqs = seen.flatMap((e) => (e.seq ? [e.seq] : []));
    expect(seqs).toEqual([...new Set(seqs)].sort((x, y) => x - y));
    expect(seqs.length).toBe(last + 1);
  });

  it("drops a phone's oldest socket over the cap and refuses sockets over the relay's limit", async () => {
    const b = await app();
    const dev = await pairDevice(b);
    const url = await listen(b);
    const open = (): Promise<WebSocket> =>
      new Promise((resolve, reject) => {
        const ws = new WebSocket(url, { headers: { authorization: `Bearer ${dev.device_jwt}` } });
        ws.once("message", () => resolve(ws));
        ws.once("error", reject);
      });
    const first = await open();
    const closed = new Promise<number>((r) => first.once("close", r));
    const rest = [await open(), await open(), await open(), await open()];
    expect(await closed).toBe(1013);
    rest.forEach((w) => w.close());
  });
});
