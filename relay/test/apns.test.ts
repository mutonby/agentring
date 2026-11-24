import { generateKeyPairSync } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import http2 from "node:http2";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { importSPKI, jwtVerify } from "jose";
import { afterEach, describe, expect, it } from "vitest";
import { buildApp } from "../src/app.js";
import { ApnsProvider, type ApnsRequest, type ApnsSender } from "../src/push/apns.js";
import { apnsRequestFor, signEvent } from "../src/push/dispatcher.js";
import type { PushResult } from "../src/push/push-provider.js";
import { AGENT, FakeLiveKit, FakePush, pairDevice, testConfig } from "./helpers.js";

class FakeApns implements ApnsSender {
  readonly topic = "dev.agentring.app";
  sent: ApnsRequest[] = [];
  next: PushResult = { ok: true };
  async send(req: ApnsRequest): Promise<PushResult> {
    this.sent.push(req);
    return this.next;
  }
}

const VOIP = "a".repeat(64);
const ALERT = "b".repeat(64);

let cleanup: Array<() => Promise<unknown> | void> = [];
afterEach(async () => {
  // Last in, first out: providers close their sessions before the fake gateway stops.
  for (const c of cleanup.reverse()) await c();
  cleanup = [];
});

const flush = () => new Promise((r) => setImmediate(r));

async function app() {
  const push = new FakePush();
  const apns = new FakeApns();
  const built = await buildApp(testConfig(), { logger: false, pushProvider: push, apnsSender: apns, livekit: new FakeLiveKit() });
  cleanup.push(() => built.app.close());
  return { ...built, push, apns };
}

async function pairIphone(b: Awaited<ReturnType<typeof app>>, tokens: Record<string, string> = { voip_token: VOIP, apns_token: ALERT }) {
  const { pairing_token } = (await b.app.inject({ method: "POST", url: "/v1/pairing-tokens", headers: AGENT })).json();
  const r = await b.app.inject({
    method: "POST",
    url: "/v1/pair",
    payload: { pairing_token, device_name: "iPhone", platform: "ios", apns_env: "sandbox", ...tokens },
  });
  expect(r.statusCode).toBe(201);
  const body = r.json();
  return { ...body, headers: { authorization: `Bearer ${body.device_jwt}` } } as {
    device_id: string;
    push_secret: string;
    push_config: unknown;
    headers: { authorization: string };
  };
}

describe("iPhone pairing and tokens", () => {
  it("pairs an iPhone with APNs push config and lists it", async () => {
    const b = await app();
    const d = await pairIphone(b);
    expect(d.push_config).toEqual({ provider: "apns", apns: { topic: "dev.agentring.app" } });
    const [dev] = (await b.app.inject({ url: "/v1/devices", headers: AGENT })).json().devices;
    expect(dev).toMatchObject({ platform: "ios", has_push_token: true });
  });

  it("an iPhone without a PushKit token can't ring yet; /v1/device/apns adds it", async () => {
    const b = await app();
    const d = await pairIphone(b, {});
    expect((await b.app.inject({ url: "/v1/devices", headers: AGENT })).json().devices[0].has_push_token).toBe(false);
    const r = await b.app.inject({ method: "POST", url: "/v1/device/apns", headers: d.headers, payload: { voip_token: VOIP } });
    expect(r.statusCode).toBe(204);
    expect((await b.app.inject({ url: "/v1/devices", headers: AGENT })).json().devices[0].has_push_token).toBe(true);
  });

  it("rejects APNs tokens from Android phones and malformed tokens", async () => {
    const b = await app();
    const android = await pairDevice(b);
    const r = await b.app.inject({ method: "POST", url: "/v1/device/apns", headers: android.headers, payload: { voip_token: VOIP } });
    expect(r.statusCode).toBe(400);
    const ios = await pairIphone(b);
    const bad = await b.app.inject({ method: "POST", url: "/v1/device/apns", headers: ios.headers, payload: { voip_token: "NOT HEX" } });
    expect(bad.statusCode).toBe(400);
    const empty = await b.app.inject({ method: "POST", url: "/v1/device/apns", headers: ios.headers, payload: {} });
    expect(empty.statusCode).toBe(400);
  });

  it("Android pairing still gets the FCM config", async () => {
    const b = await app();
    const t =(await b.app.inject({ method: "POST", url: "/v1/pairing-tokens", headers: AGENT })).json();
    const r = await b.app.inject({
      method: "POST",
      url: "/v1/pair",
      payload: { pairing_token: t.pairing_token, device_name: "Pixel", platform: "android", fcm_token: "f" },
    });
    expect(r.json().push_config.provider).toBe("fcm");
  });
});

describe("iPhone pushes", () => {
  it("call.invite is a VoIP push signed like FCM; Android still gets FCM", async () => {
    const b = await app();
    const ios = await pairIphone(b);
    await pairDevice(b);
    const r = await b.app.inject({ method: "POST", url: "/v1/calls", headers: AGENT, payload: { reason: "Deploy ready" } });
    expect(r.statusCode).toBe(201);
    await flush();

    expect(b.push.sent.map((s) => JSON.parse(s.payload.event!).type)).toEqual(["call.invite"]);
    expect(b.apns.sent).toHaveLength(1);
    const req = b.apns.sent[0]!;
    expect(req).toMatchObject({ token: VOIP, env: "sandbox", pushType: "voip", priority: 10 });
    const p = req.payload as Record<string, string>;
    const invite = JSON.parse(p.event!);
    expect(invite).toMatchObject({ type: "call.invite", call_id: r.json().call.call_id });
    expect(req.expiration).toBe(Math.ceil(invite.expires_at / 1000));
    expect(p.aps).toEqual({});
    expect(p.sig).toBe(signEvent(ios.push_secret, p.event!));
  });

  it("msg.new is an alert to the APNs token, text only inside the signed event", async () => {
    const b = await app();
    const ios = await pairIphone(b);
    await b.app.inject({ method: "POST", url: "/v1/messages", headers: AGENT, payload: { text: "secret plan" } });
    await flush();
    const req = b.apns.sent[0]!;
    expect(req).toMatchObject({ token: ALERT, pushType: "alert", priority: 10 });
    const p = req.payload as { aps: Record<string, unknown>; event: string; sig: string };
    expect(p.aps).toMatchObject({ alert: { title: "TestAgent", "loc-key": "PUSH_MSG_NEW" }, "mutable-content": 1, category: "MSG_NEW" });
    expect(JSON.stringify(p.aps)).not.toContain("secret plan");
    expect(JSON.parse(p.event).text).toBe("secret plan");
    expect(p.sig).toBe(signEvent(ios.push_secret, p.event));
  });

  it("a long message fits APNs' 4 KB", async () => {
    const b = await app();
    await pairIphone(b);
    await b.app.inject({ method: "POST", url: "/v1/messages", headers: AGENT, payload: { text: "ñ".repeat(3900) } });
    await flush();
    const p = b.apns.sent[0]!.payload as { event: string };
    expect(Buffer.byteLength(JSON.stringify(b.apns.sent[0]!.payload))).toBeLessThanOrEqual(4096);
    expect(JSON.parse(p.event).truncated).toBe(true);
  });

  it("missed call alert outlives the ring; call end is a silent push", async () => {
    const b = await app();
    const ios = await pairIphone(b);
    const call = (await b.app.inject({ method: "POST", url: "/v1/calls", headers: AGENT, payload: { reason: "x" } })).json().call;
    await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/end`, headers: AGENT });
    await flush();
    const types = b.apns.sent.map((s) => [JSON.parse((s.payload as { event: string }).event).type, s.pushType, s.token]);
    expect(types[0]).toEqual(["call.invite", "voip", VOIP]);
    const rest = types.slice(1);
    expect(rest.every(([, , token]) => token === ALERT)).toBe(true);
    expect(rest.some(([, kind]) => kind === "voip")).toBe(false);
    expect(ios.device_id).toBeTruthy();
  });

  it("a stale VoIP token is forgotten, the alert token stays", async () => {
    const b = await app();
    await pairIphone(b);
    b.apns.next = { ok: false, unregistered: true, error: "410 Unregistered" };
    await b.app.inject({ method: "POST", url: "/v1/calls", headers: AGENT, payload: { reason: "x" } });
    await flush();
    await flush();
    const row = b.ctx.db.prepare("SELECT apns_token, voip_token FROM devices").get() as { apns_token: string; voip_token: string | null };
    expect(row).toEqual({ apns_token: ALERT, voip_token: null });
  });

  it("revoking an iPhone drops its tokens", async () => {
    const b = await app();
    const d = await pairIphone(b);
    await b.app.inject({ method: "DELETE", url: `/v1/devices/${d.device_id}`, headers: AGENT });
    await b.app.inject({ method: "POST", url: "/v1/messages", headers: AGENT, payload: { text: "hi" } });
    await flush();
    expect(b.apns.sent).toHaveLength(0);
  });
});

describe("apnsRequestFor", () => {
  const data = { v: "1", relay_id: "rly_x", event: "{}", sig: "s" };
  const tokens = { apns: ALERT, voip: VOIP, env: "production" as const };
  const rule = { ttlSeconds: 60, highPriority: true };

  it("never sends anything but call.invite to the VoIP token", () => {
    const events = [
      { type: "msg.new", id: "e", ts: 0, seq: 1, msg_id: "m", from: "agent", text: "t" },
      { type: "call.missed", id: "e", ts: 0, seq: 1, call_id: "c" },
      { type: "call.accept", id: "e", ts: 0, seq: 1, call_id: "c", by: "user", device_id: "d" },
      { type: "call.end", id: "e", ts: 0, seq: 1, call_id: "c", by: "agent", reason: "hangup" },
    ];
    for (const e of events) {
      const req = apnsRequestFor(e as never, rule, data, tokens, "A", 1_000_000);
      expect(req?.pushType).not.toBe("voip");
      expect(req?.token).toBe(ALERT);
    }
  });

  it("silent pushes use priority 5", () => {
    const req = apnsRequestFor({ type: "call.end", id: "e", ts: 0, seq: 1, call_id: "c", by: "agent", reason: "hangup" } as never, rule, data, tokens, "A");
    expect(req).toMatchObject({ pushType: "background", priority: 5, payload: { aps: { "content-available": 1 } } });
  });

  it("skips an event when the needed token is missing", () => {
    const invite = { type: "call.invite", id: "e", ts: 0, seq: 1, call_id: "c", direction: "agent_to_user", agent_name: "A", reason: "", priority: "normal", expires_at: 5000 };
    expect(apnsRequestFor(invite as never, rule, data, { ...tokens, voip: null }, "A")).toBeUndefined();
  });
});

describe("ApnsProvider over HTTP/2", () => {
  async function fakeApple(status: number, body = "") {
    const seen: Array<{ headers: http2.IncomingHttpHeaders; body: string }> = [];
    const server = http2.createServer();
    server.on("stream", (stream, headers) => {
      let data = "";
      stream.setEncoding("utf8");
      stream.on("data", (c: string) => (data += c));
      stream.on("end", () => {
        seen.push({ headers, body: data });
        stream.respond({ ":status": status });
        stream.end(body);
      });
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    cleanup.push(() => new Promise((r) => server.close(r)));
    return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, seen };
  }

  function provider(url: string) {
    const { privateKey, publicKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
    const dir = mkdtempSync(join(tmpdir(), "apns-"));
    const keyFile = join(dir, "AuthKey.p8");
    writeFileSync(keyFile, privateKey.export({ type: "pkcs8", format: "pem" }));
    const p = new ApnsProvider({ keyFile, keyId: "ABC123DEFG", teamId: "TEAM123456", topic: "dev.agentring.app", hosts: { production: url, sandbox: url } });
    cleanup.push(() => p.close());
    return { p, publicPem: publicKey.export({ type: "spki", format: "pem" }).toString() };
  }

  it("sends a VoIP push with a valid ES256 provider token", async () => {
    const apple = await fakeApple(200);
    const { p, publicPem } = provider(apple.url);
    const res = await p.send({ token: VOIP, env: "production", pushType: "voip", priority: 10, expiration: 123, payload: { aps: {}, v: "1" } });
    expect(res).toEqual({ ok: true });
    const { headers, body } = apple.seen[0]!;
    expect(headers[":path"]).toBe(`/3/device/${VOIP}`);
    expect(headers["apns-topic"]).toBe("dev.agentring.app.voip");
    expect(headers["apns-push-type"]).toBe("voip");
    expect(headers["apns-priority"]).toBe("10");
    expect(headers["apns-expiration"]).toBe("123");
    expect(JSON.parse(body)).toEqual({ aps: {}, v: "1" });
    const jwt = String(headers.authorization).replace(/^bearer /, "");
    const { payload, protectedHeader } = await jwtVerify(jwt, await importSPKI(publicPem, "ES256"));
    expect(protectedHeader).toMatchObject({ alg: "ES256", kid: "ABC123DEFG" });
    expect(payload.iss).toBe("TEAM123456");
  });

  it("alerts go to the bare topic; 410 marks the token unregistered", async () => {
    const apple = await fakeApple(410, JSON.stringify({ reason: "Unregistered" }));
    const { p } = provider(apple.url);
    const res = await p.send({ token: ALERT, env: "sandbox", pushType: "alert", priority: 10, expiration: 0, collapseId: "c1", payload: { aps: {} } });
    expect(res).toMatchObject({ ok: false, unregistered: true });
    expect(apple.seen[0]!.headers["apns-topic"]).toBe("dev.agentring.app");
    expect(apple.seen[0]!.headers["apns-collapse-id"]).toBe("c1");
  });

  it("a server error is not mistaken for a dead token", async () => {
    const apple = await fakeApple(500, JSON.stringify({ reason: "InternalServerError" }));
    const { p } = provider(apple.url);
    const res = await p.send({ token: ALERT, env: "production", pushType: "alert", priority: 10, expiration: 0, payload: { aps: {} } });
    expect(res).toMatchObject({ ok: false, unregistered: false });
  });

  it("an unreachable gateway returns an error instead of throwing", async () => {
    const { p } = provider("http://127.0.0.1:1");
    const res = await p.send({ token: ALERT, env: "production", pushType: "alert", priority: 10, expiration: 0, payload: { aps: {} } });
    expect(res.ok).toBe(false);
  });
});
