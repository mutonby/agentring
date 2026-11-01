import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AddressInfo } from "node:net";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import WebSocket from "ws";
import { buildHub, type Hub } from "../src/hub/hub.js";
import type { FastifyInstance } from "fastify";
import { FakeLiveKit, FakePush, testConfig } from "./helpers.js";

const ADMIN_TOKEN = "admin-token-0123456789-0123456789-abcdef";
const ADMIN = { authorization: `Bearer ${ADMIN_TOKEN}` };

let dir: string;
let app: FastifyInstance;
let hub: Hub;
let url: string;
let livekit: FakeLiveKit;
let push: FakePush;

async function start(idleUnloadMs?: number) {
  livekit = new FakeLiveKit();
  push = new FakePush();
  ({ app, hub } = await buildHub(testConfig({ dataDir: dir }), {
    adminToken: ADMIN_TOKEN,
    logger: false,
    livekit,
    pushProvider: push,
    idleUnloadMs,
  }));
  await app.listen({ host: "127.0.0.1", port: 0 });
  url = `http://127.0.0.1:${(app.server.address() as AddressInfo).port}`;
}

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), "agentring-hub-"));
  await start();
});

afterEach(async () => {
  await app.close();
  rmSync(dir, { recursive: true, force: true });
});

async function createTenant(limits?: Record<string, number | null>) {
  const r = await app.inject({ method: "POST", url: "/admin/v1/tenants", headers: ADMIN, payload: { agent_name: "Claude", limits } });
  expect(r.statusCode).toBe(201);
  const body = r.json() as { tenant: { tenant_id: string; base_url: string }; api_key: string };
  return { tid: body.tenant.tenant_id, apiKey: body.api_key, tenant: body.tenant, auth: { authorization: `Bearer ${body.api_key}` } };
}

async function http(method: string, path: string, headers: Record<string, string> = {}, body?: unknown) {
  const res = await fetch(url + path, {
    method,
    headers: { ...headers, ...(body !== undefined ? { "content-type": "application/json" } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  return { status: res.status, json: text ? JSON.parse(text) : undefined };
}

async function pair(tid: string, name = "Pixel") {
  const t = await app.inject({ method: "POST", url: `/admin/v1/tenants/${tid}/pairing-tokens`, headers: ADMIN });
  expect(t.statusCode).toBe(201);
  const { pairing_token, qr_payload } = t.json();
  const r = await http("POST", `/t/${tid}/v1/pair`, {}, { pairing_token, device_name: name, platform: "android", fcm_token: "f" });
  return { ...r, qr: JSON.parse(qr_payload), headers: { authorization: `Bearer ${r.json?.device_jwt}` } };
}

describe("hub mode", () => {
  it("creates tenants whose base URL serves the normal relay API", async () => {
    const a = await createTenant();
    expect(a.tid).toMatch(/^tnt_[0-9A-Z]{26}$/);
    expect(a.tenant.base_url).toBe(`https://relay.test/t/${a.tid}`);
    expect(a.apiKey).toMatch(/^ak_/);

    const s = await http("GET", `/t/${a.tid}/v1/status`, a.auth);
    expect(s.status).toBe(200);
    expect(s.json.agent_name).toBe("Claude");
    expect(s.json.device_count).toBe(0);
    expect((await http("GET", `/t/${a.tid}/healthz`)).json).toEqual({ ok: true });
  });

  it("isolates tenants: keys, device tokens and data", async () => {
    const a = await createTenant();
    const b = await createTenant();
    expect((await http("GET", `/t/${a.tid}/v1/status`, b.auth)).status).toBe(401);

    const dev = await pair(a.tid);
    expect(dev.status).toBe(201);
    expect(dev.qr.relay_url).toBe(`https://relay.test/t/${a.tid}`);
    expect((await http("GET", `/t/${a.tid}/v1/status`, dev.headers)).status).toBe(200);
    expect((await http("GET", `/t/${b.tid}/v1/status`, dev.headers)).status).toBe(401);

    await http("POST", `/t/${a.tid}/v1/messages`, a.auth, { text: "hi A" });
    const msgsB = await http("GET", `/t/${b.tid}/v1/messages`, b.auth);
    expect(msgsB.json.messages).toEqual([]);
    const msgsA = await http("GET", `/t/${a.tid}/v1/messages`, a.auth);
    expect(msgsA.json.messages.map((m: { text: string }) => m.text)).toEqual(["hi A"]);
  });

  it("answers 404 for unknown tenants and for the tenant-less API", async () => {
    expect((await http("GET", "/t/tnt_00000000000000000000000000/v1/status")).json.error.code).toBe("not_found");
    expect((await http("GET", "/v1/status")).status).toBe(404);
    expect((await http("GET", "/healthz")).json.hub).toBe(true);
  });

  it("serves the tenant's WebSocket under its path", async () => {
    const a = await createTenant();
    const ws = new WebSocket(`${url.replace("http", "ws")}/t/${a.tid}/v1/ws?after_seq=0`, { headers: a.auth });
    const hello = await new Promise<{ type: string }>((resolve, reject) => {
      ws.once("message", (d) => resolve(JSON.parse(String(d))));
      ws.once("error", reject);
    });
    expect(hello.type).toBe("ws.hello");
    const live = new Promise<{ type: string; text?: string }>((resolve) =>
      ws.on("message", (d) => {
        const e = JSON.parse(String(d));
        if (e.type === "msg.new") resolve(e);
      }),
    );
    await http("POST", `/t/${a.tid}/v1/messages`, a.auth, { text: "over ws" });
    expect((await live).text).toBe("over ws");
    ws.close();

    const bad = new WebSocket(`${url.replace("http", "ws")}/t/${a.tid}/v1/ws`, { headers: { authorization: "Bearer ak_nope" } });
    const status = await new Promise<number>((resolve) => bad.once("unexpected-response", (_req, res) => resolve(res.statusCode ?? 0)));
    expect(status).toBe(401);
  });

  it("enforces max_devices and messages_per_day", async () => {
    const a = await createTenant({ max_devices: 1, messages_per_day: 2 });
    expect((await pair(a.tid)).status).toBe(201);
    const second = await pair(a.tid, "Second");
    expect(second.status).toBe(402);
    expect(second.json.error.code).toBe("quota_exceeded");

    expect((await http("POST", `/t/${a.tid}/v1/messages`, a.auth, { text: "1" })).status).toBe(201);
    expect((await http("POST", `/t/${a.tid}/v1/messages`, a.auth, { text: "2" })).status).toBe(201);
    expect((await http("POST", `/t/${a.tid}/v1/messages`, a.auth, { text: "3" })).status).toBe(402);

    // Raising the limit takes effect at once.
    const p = await app.inject({ method: "PATCH", url: `/admin/v1/tenants/${a.tid}`, headers: ADMIN, payload: { limits: { messages_per_day: 10 } } });
    expect(p.json().limits).toEqual({ max_devices: 1, call_seconds_per_month: null, messages_per_day: 10 });
    expect((await http("POST", `/t/${a.tid}/v1/messages`, a.auth, { text: "3" })).status).toBe(201);
    expect(p.json().usage.devices).toBe(1);
  });

  it("refuses calls without call minutes and ends an active call when they run out", async () => {
    const a = await createTenant({ call_seconds_per_month: 0 });
    const dev = await pair(a.tid);
    const refused = await http("POST", `/t/${a.tid}/v1/calls`, a.auth, { reason: "test" });
    expect(refused.status).toBe(402);

    await app.inject({ method: "PATCH", url: `/admin/v1/tenants/${a.tid}`, headers: ADMIN, payload: { limits: { call_seconds_per_month: 2 } } });
    const call = await http("POST", `/t/${a.tid}/v1/calls`, a.auth, { reason: "test" });
    expect(call.status).toBe(201);
    const id = call.json.call.call_id as string;
    expect(livekit.created).toEqual([`${a.tid}.${id}`]);
    expect(call.json.join.room).toBe(`${a.tid}.${id}`);

    expect((await http("POST", `/t/${a.tid}/v1/calls/${id}/accept`, dev.headers)).status).toBe(200);
    await new Promise((r) => setTimeout(r, 2300));
    const ended = await http("GET", `/t/${a.tid}/v1/calls/${id}`, a.auth);
    expect(ended.json.state).toBe("ended");
    expect(ended.json.end_reason).toBe("quota");
    const t = await app.inject({ method: "GET", url: `/admin/v1/tenants/${a.tid}`, headers: ADMIN });
    expect(t.json().usage.call_seconds).toBeGreaterThanOrEqual(2);
    expect(t.json().usage.calls).toBe(1);
  });

  it("applies a lowered call limit to the active call at once", async () => {
    const a = await createTenant({ call_seconds_per_month: 3600 });
    const dev = await pair(a.tid);
    const call = await http("POST", `/t/${a.tid}/v1/calls`, a.auth, { reason: "test" });
    const id = call.json.call.call_id as string;
    await http("POST", `/t/${a.tid}/v1/calls/${id}/accept`, dev.headers);
    await new Promise((r) => setTimeout(r, 1100));
    await app.inject({ method: "PATCH", url: `/admin/v1/tenants/${a.tid}`, headers: ADMIN, payload: { limits: { call_seconds_per_month: 1 } } });
    const ended = await http("GET", `/t/${a.tid}/v1/calls/${id}`, a.auth);
    expect(ended.json.state).toBe("ended");
    expect(ended.json.end_reason).toBe("quota");
  });

  it("does not bring back a tenant that is being deleted", async () => {
    const a = await createTenant();
    await http("GET", `/t/${a.tid}/v1/status`, a.auth); // loaded
    const del = hub.delete(a.tid);
    const during = await http("GET", `/t/${a.tid}/v1/status`, a.auth);
    await del;
    expect(during.status).toBe(404);
    expect(existsSync(join(dir, "tenants", a.tid))).toBe(false);
    expect((await http("GET", `/t/${a.tid}/v1/status`, a.auth)).status).toBe(404);
  });

  it("routes the shared LiveKit webhook to the tenant in the room name", async () => {
    const a = await createTenant();
    const dev = await pair(a.tid);
    const call = await http("POST", `/t/${a.tid}/v1/calls`, a.auth, { reason: "x" });
    const id = call.json.call.call_id;
    await http("POST", `/t/${a.tid}/v1/calls/${id}/accept`, dev.headers);

    const hook = (room: string, auth = "ok", extra: Record<string, string> = {}) =>
      app.inject({
        method: "POST",
        url: "/v1/livekit/webhook",
        headers: { "content-type": "application/webhook+json", authorization: auth, ...extra },
        payload: JSON.stringify({ event: "room_finished", room: { name: room } }),
      });
    expect((await hook(`${a.tid}.${id}`, "bad")).statusCode).toBe(401);
    expect((await hook(`${a.tid}.${id}`, "ok", { "x-forwarded-for": "1.2.3.4" })).statusCode).toBe(404);
    expect((await hook("someone-else")).statusCode).toBe(200);
    expect((await hook(`${a.tid}.${id}`)).statusCode).toBe(200);
    expect((await http("GET", `/t/${a.tid}/v1/calls/${id}`, a.auth)).json.state).toBe("ended");
  });

  it("disables, rotates keys and deletes tenants", async () => {
    const a = await createTenant();
    const resolve = (api_key: string) => app.inject({ method: "POST", url: "/admin/v1/resolve-key", headers: ADMIN, payload: { api_key } });
    expect((await resolve(a.apiKey)).json()).toEqual({ tenant_id: a.tid, base_url: `https://relay.test/t/${a.tid}` });
    expect((await resolve("ak_unknown")).statusCode).toBe(404);

    const rot = await app.inject({ method: "POST", url: `/admin/v1/tenants/${a.tid}/api-key`, headers: ADMIN });
    const newKey = rot.json().api_key as string;
    expect((await http("GET", `/t/${a.tid}/v1/status`, a.auth)).status).toBe(401);
    expect((await http("GET", `/t/${a.tid}/v1/status`, { authorization: `Bearer ${newKey}` })).status).toBe(200);

    await app.inject({ method: "PATCH", url: `/admin/v1/tenants/${a.tid}`, headers: ADMIN, payload: { disabled: true } });
    const off = await http("GET", `/t/${a.tid}/v1/status`, { authorization: `Bearer ${newKey}` });
    expect(off.status).toBe(403);
    expect(off.json.error.code).toBe("tenant_disabled");
    expect((await resolve(newKey)).statusCode).toBe(403);

    expect(existsSync(join(dir, "tenants", a.tid, "relay.db"))).toBe(true);
    expect((await app.inject({ method: "DELETE", url: `/admin/v1/tenants/${a.tid}`, headers: ADMIN })).statusCode).toBe(204);
    expect(existsSync(join(dir, "tenants", a.tid))).toBe(false);
    expect((await http("GET", `/t/${a.tid}/v1/status`)).status).toBe(404);
    expect((await app.inject({ method: "GET", url: `/admin/v1/tenants/${a.tid}`, headers: ADMIN })).statusCode).toBe(404);
  });

  it("lists and revokes a tenant's devices through the admin API", async () => {
    const a = await createTenant();
    const dev = await pair(a.tid);
    const list = await app.inject({ method: "GET", url: `/admin/v1/tenants/${a.tid}/devices`, headers: ADMIN });
    expect(list.json().devices).toHaveLength(1);
    const del = await app.inject({ method: "DELETE", url: `/admin/v1/tenants/${a.tid}/devices/${dev.json.device_id}`, headers: ADMIN });
    expect(del.statusCode).toBe(204);
    expect((await http("GET", `/t/${a.tid}/v1/status`, dev.headers)).status).toBe(401);
  });

  it("protects the admin API", async () => {
    expect((await app.inject({ method: "POST", url: "/admin/v1/tenants" })).statusCode).toBe(401);
    expect((await app.inject({ method: "POST", url: "/admin/v1/tenants", headers: { authorization: "Bearer wrong" } })).statusCode).toBe(401);
    const proxied = await app.inject({ method: "POST", url: "/admin/v1/tenants", headers: { ...ADMIN, "x-forwarded-for": "1.2.3.4" } });
    expect(proxied.statusCode).toBe(404);
    // Through the real server, too (the public proxy adds X-Forwarded-For).
    expect((await http("POST", "/admin/v1/tenants", { ...ADMIN, "x-forwarded-for": "1.2.3.4" }, {})).status).toBe(404);
  });

  it("unloads idle tenants and reloads them with their data", async () => {
    const a = await createTenant();
    await http("POST", `/t/${a.tid}/v1/messages`, a.auth, { text: "kept" });
    await hub.sweep(Date.now() + 60 * 60_000);
    // Reloaded on the next request, data intact.
    const msgs = await http("GET", `/t/${a.tid}/v1/messages`, a.auth);
    expect(msgs.json.messages.map((m: { text: string }) => m.text)).toEqual(["kept"]);
  });

  it("keeps tenants across a restart and resumes their open calls", async () => {
    const a = await createTenant();
    await pair(a.tid);
    const call = await http("POST", `/t/${a.tid}/v1/calls`, a.auth, { reason: "x" });
    expect(call.status).toBe(201);
    await app.close();
    await start();
    const c = await http("GET", `/t/${a.tid}/v1/calls/${call.json.call.call_id}`, a.auth);
    expect(c.json.state).toBe("ringing");
  });
});
