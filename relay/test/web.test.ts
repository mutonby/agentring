import { afterEach, describe, expect, it } from "vitest";
import WebSocket from "ws";
import { AGENT, setup } from "./helpers.js";

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

/** Pairs like the browser client does: platform web, no FCM token. */
async function pairBrowser(b: Awaited<ReturnType<typeof app>>) {
  const t = await b.app.inject({ method: "POST", url: "/v1/pairing-tokens", headers: AGENT });
  const r = await b.app.inject({
    method: "POST",
    url: "/v1/pair",
    payload: { pairing_token: t.json().pairing_token, device_name: "Browser", platform: "web", app_version: "0.2.0-web" },
  });
  expect(r.statusCode).toBe(201);
  return r.json() as { device_id: string; device_jwt: string };
}

describe("browser client", () => {
  it("is served at /web/ with a CSP that allows only the relay and LiveKit", async () => {
    const b = await app({
      livekit: { publicUrl: "wss://lk.example.com", apiUrl: "http://livekit:7880", apiKey: "k", apiSecret: "s" },
    });
    const redirect = await b.app.inject({ method: "GET", url: "/web" });
    expect(redirect.statusCode).toBe(301);
    expect(redirect.headers.location).toBe("/web/");

    const page = await b.app.inject({ method: "GET", url: "/web/" });
    expect(page.statusCode).toBe(200);
    expect(page.headers["content-type"]).toContain("text/html");
    expect(page.body).toContain('<script type="module" src="app.js">');
    const csp = String(page.headers["content-security-policy"]);
    expect(csp).toContain("script-src 'self'");
    expect(csp).toContain("connect-src 'self' https://relay.test wss://relay.test https://lk.example.com wss://lk.example.com");
    expect(page.headers["x-content-type-options"]).toBe("nosniff");

    const js = await b.app.inject({ method: "GET", url: "/web/app.js" });
    expect(js.statusCode).toBe(200);
    expect(js.headers["content-type"]).toContain("text/javascript");
  });

  it("serves only its own files", async () => {
    const b = await app();
    for (const url of ["/web/../src/app.ts", "/web/%2e%2e/package.json", "/web/vendor/", "/web/nope.js"]) {
      expect((await b.app.inject({ method: "GET", url })).statusCode, url).toBe(404);
    }
  });

  it("pairs as a web device without a push token, and calls still ring it over the WebSocket", async () => {
    const b = await app();
    const d = await pairBrowser(b);
    const devices = (await b.app.inject({ method: "GET", url: "/v1/devices", headers: AGENT })).json().devices;
    expect(devices).toEqual([expect.objectContaining({ device_id: d.device_id, platform: "web", has_push_token: false })]);

    const created = await b.app.inject({ method: "POST", url: "/v1/calls", headers: AGENT, payload: { reason: "test" } });
    expect(created.statusCode).toBe(201);
    expect(b.push.sent).toEqual([]); // nothing to push to
    const events = (
      await b.app.inject({ method: "GET", url: "/v1/events", headers: { authorization: `Bearer ${d.device_jwt}` } })
    ).json().events;
    expect(events.map((e: { type: string }) => e.type)).toContain("call.invite");
  });

  it("rejects other platforms", async () => {
    const b = await app();
    const t = await b.app.inject({ method: "POST", url: "/v1/pairing-tokens", headers: AGENT });
    const r = await b.app.inject({
      method: "POST",
      url: "/v1/pair",
      payload: { pairing_token: t.json().pairing_token, device_name: "x", platform: "ios" },
    });
    expect(r.statusCode).toBe(400);
  });
});

describe("websocket auth by subprotocol (browsers)", () => {
  async function listen(b: Awaited<ReturnType<typeof app>>) {
    await b.app.listen({ port: 0, host: "127.0.0.1" });
    return `ws://127.0.0.1:${(b.app.server.address() as { port: number }).port}/v1/ws`;
  }

  it("accepts the token as bearer.<token> and selects agentring.v1", async () => {
    const b = await app();
    const d = await pairBrowser(b);
    const ws = new WebSocket(await listen(b), ["agentring.v1", `bearer.${d.device_jwt}`]);
    cleanup.push(async () => ws.close());
    const hello = await new Promise<Record<string, unknown>>((r) => ws.once("message", (m) => r(JSON.parse(m.toString()))));
    expect(hello).toMatchObject({ type: "ws.hello", relay_id: b.ctx.relayId });
    expect(ws.protocol).toBe("agentring.v1"); // never the bearer one
  });

  it("rejects a bad or missing subprotocol token", async () => {
    const b = await app();
    const url = await listen(b);
    for (const protocols of [["agentring.v1", "bearer.not-a-jwt"], ["agentring.v1"]]) {
      const ws = new WebSocket(url, protocols);
      const status = await new Promise<number | undefined>((r) => ws.on("unexpected-response", (_q, res) => r(res.statusCode)));
      expect(status, protocols.join(",")).toBe(401);
    }
  });

  it("uses the Authorization header when both are present", async () => {
    const b = await app();
    const d = await pairBrowser(b);
    const ws = new WebSocket(await listen(b), ["agentring.v1", `bearer.${d.device_jwt}`], {
      headers: { authorization: "Bearer ak_wrong" },
    });
    const status = await new Promise<number | undefined>((r) => ws.on("unexpected-response", (_q, res) => r(res.statusCode)));
    expect(status).toBe(401);
  });

  it("does not read the token from the subprotocol header on plain requests", async () => {
    const b = await app();
    const d = await pairBrowser(b);
    const r = await b.app.inject({
      method: "GET",
      url: "/v1/status",
      headers: { "sec-websocket-protocol": `agentring.v1, bearer.${d.device_jwt}` },
    });
    expect(r.statusCode).toBe(401);
  });
});
