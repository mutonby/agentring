import { afterEach, describe, expect, it, vi } from "vitest";
import WebSocket from "ws";
import { AGENT, pairDevice, setup } from "./helpers.js";

let cleanup: Array<() => Promise<unknown>> = [];
afterEach(async () => {
  vi.useRealTimers();
  for (const c of cleanup) await c();
  cleanup = [];
});

async function app(over = {}) {
  const b = await setup(over);
  cleanup.push(() => b.app.close());
  return b;
}

const flush = () => new Promise((r) => setImmediate(r));

async function placeCall(b: Awaited<ReturnType<typeof app>>, payload: object = { reason: "Deploy ready" }) {
  return b.app.inject({ method: "POST", url: "/v1/calls", headers: AGENT, payload });
}

function eventsOf(b: Awaited<ReturnType<typeof app>>) {
  return b.ctx.events.list(0, 500).events;
}

describe("calls", () => {
  it("agent call: invite pushed with TTL until expiry, ringing, accept with join info, hang up", async () => {
    const b = await app();
    const dev = await pairDevice(b);

    const r = await placeCall(b, { reason: "Deploy ready", priority: "urgent" });
    expect(r.statusCode).toBe(201);
    const { call, join } = r.json();
    expect(call).toMatchObject({ direction: "agent_to_user", state: "ringing", priority: "urgent", reason: "Deploy ready", agent_name: "TestAgent" });
    expect(join).toMatchObject({ room: call.call_id, identity: "agent", livekit_url: "wss://lk.test" });
    expect(b.livekit.created).toEqual([call.call_id]);

    await flush();
    const invitePush = b.push.sent.find((s) => JSON.parse(s.payload.event!).type === "call.invite")!;
    expect(invitePush.opts.highPriority).toBe(true);
    expect(invitePush.opts.ttlSeconds).toBeGreaterThan(25);
    expect(invitePush.opts.ttlSeconds).toBeLessThanOrEqual(30);
    const invite = JSON.parse(invitePush.payload.event!);
    expect(invite).toMatchObject({ call_id: call.call_id, expires_at: call.created_at + 30_000 });

    // ringing is idempotent per device
    for (let i = 0; i < 2; i++) {
      const rr = await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/ringing`, headers: dev.headers });
      expect(rr.statusCode).toBe(204);
    }
    expect(eventsOf(b).filter((e) => e.type === "call.ringing")).toHaveLength(1);

    // status shows the open call
    const st = (await b.app.inject({ url: "/v1/status", headers: AGENT })).json();
    expect(st.active_call.call_id).toBe(call.call_id);

    // the caller can't accept its own call
    expect((await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/accept`, headers: AGENT })).statusCode).toBe(403);

    const acc = await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/accept`, headers: dev.headers });
    expect(acc.statusCode).toBe(200);
    expect(acc.json().call).toMatchObject({ state: "active", device_id: dev.device_id });
    expect(acc.json().join.identity).toBe(`device_${dev.device_id}`);

    // the same phone accepting again (e.g. after a drop) rejoins with a new token
    const again = await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/accept`, headers: dev.headers });
    expect(again.statusCode).toBe(200);
    expect(again.json().join.identity).toBe(`device_${dev.device_id}`);

    // busy while active
    expect((await placeCall(b)).json().error.code).toBe("busy");

    const end = await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/end`, headers: dev.headers });
    expect(end.json()).toMatchObject({ state: "ended", ended_by: "user", end_reason: "hangup" });
    expect(end.json().duration_ms).toBeGreaterThanOrEqual(0);
    expect(b.livekit.deleted).toEqual([call.call_id]);
    // idempotent
    expect((await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/end`, headers: AGENT })).json().state).toBe("ended");

    const types = eventsOf(b).map((e) => e.type);
    expect(types).toEqual(["call.invite", "call.ringing", "call.accept", "call.end"]);
    await flush();
    // accept/decline don't affect other phones in a group call, so they aren't pushed
    const pushed = b.push.sent.map((s) => JSON.parse(s.payload.event!).type);
    expect(pushed).toEqual(["call.invite", "call.end"]);
  });

  it("group call: every phone rings, any may answer while it rings, the call ends with the last one", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const b = await app({ ringTimeoutS: 30 });
    const a = await pairDevice(b, "A");
    const c = await pairDevice(b, "B", "fcm-tok-2");
    const d = await pairDevice(b, "C", "fcm-tok-3");
    const { call } = (await placeCall(b)).json();
    const post = (h: { authorization: string }, action: string) =>
      b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/${action}`, headers: h });

    // C declines: A and B keep ringing, the call is still ringing
    expect((await post(d.headers, "decline")).json().state).toBe("ringing");
    // A answers, B joins later while it still rings
    expect((await post(a.headers, "accept")).json().call.state).toBe("active");
    // A self-hosted relay has no call-time limit, so nothing to count down.
    const got = await b.app.inject({ method: "GET", url: `/v1/calls/${call.call_id}`, headers: a.headers });
    expect(got.json().state).toBe("active");
    expect(got.json().time_left_ms).toBeUndefined();
    vi.advanceTimersByTime(10_000);
    const joinB = await post(c.headers, "accept");
    expect(joinB.statusCode).toBe(200);
    expect(joinB.json().join.identity).toBe(`device_${c.device_id}`);
    // C declined: it can't come back in
    expect((await post(d.headers, "accept")).statusCode).toBe(409);

    // A hangs up: B and the agent stay on the call
    expect((await post(a.headers, "end")).json().state).toBe("active");
    expect(b.livekit.deleted).toEqual([]);
    // B leaves the room (LiveKit webhook): last phone gone -> the call ends
    await b.app.inject({
      method: "POST",
      url: "/v1/livekit/webhook",
      headers: { authorization: "ok", "content-type": "application/webhook+json" },
      payload: JSON.stringify({ event: "participant_left", room: { name: call.call_id }, participant: { identity: `device_${c.device_id}` } }),
    });
    expect(b.ctx.calls!.get(call.call_id)).toMatchObject({ state: "ended", ended_by: "relay", end_reason: "room_closed" });
    expect(b.livekit.deleted).toEqual([call.call_id]);
    const types = eventsOf(b).map((e) => e.type);
    expect(types).toEqual(["call.invite", "call.decline", "call.accept", "call.accept", "call.end"]);
  });

  it("group call: answering is only possible while it rings, and all declines end it", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const b = await app({ ringTimeoutS: 30 });
    const a = await pairDevice(b, "A");
    const c = await pairDevice(b, "B", "fcm-tok-2");
    const first = (await placeCall(b)).json().call;
    await b.app.inject({ method: "POST", url: `/v1/calls/${first.call_id}/accept`, headers: a.headers });
    vi.advanceTimersByTime(31_000);
    // B's ring window is over
    const late = await b.app.inject({ method: "POST", url: `/v1/calls/${first.call_id}/accept`, headers: c.headers });
    expect(late.statusCode).toBe(409);
    // A rejoining after the window is fine (it was on the call)
    expect((await b.app.inject({ method: "POST", url: `/v1/calls/${first.call_id}/accept`, headers: a.headers })).statusCode).toBe(200);
    // A leaves after the window -> nobody left, the call ends
    expect((await b.app.inject({ method: "POST", url: `/v1/calls/${first.call_id}/end`, headers: a.headers })).json().state).toBe("ended");

    const second = (await placeCall(b)).json().call;
    await b.app.inject({ method: "POST", url: `/v1/calls/${second.call_id}/decline`, headers: a.headers });
    expect(b.ctx.calls!.get(second.call_id)!.state).toBe("ringing");
    const r = await b.app.inject({ method: "POST", url: `/v1/calls/${second.call_id}/decline`, headers: c.headers });
    expect(r.json().state).toBe("declined");
    expect(b.livekit.deleted).toContain(second.call_id);
  });

  it("join: a phone that missed its ring, declined or hung up joins while the call is active", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const b = await app({ ringTimeoutS: 30 });
    const a = await pairDevice(b, "A");
    const c = await pairDevice(b, "B", "fcm-tok-2");
    const d = await pairDevice(b, "C", "fcm-tok-3");
    const { call } = (await placeCall(b)).json();
    const post = (h: { authorization: string }, action: string, id = call.call_id) =>
      b.app.inject({ method: "POST", url: `/v1/calls/${id}/${action}`, headers: h });

    // C declines, A answers, B lets it ring out
    await post(d.headers, "decline");
    await post(a.headers, "accept");
    vi.advanceTimersByTime(31_000);
    expect(b.ctx.calls!.get(call.call_id)!.state).toBe("active");
    // accept is only for ringing phones; join works at any time while active
    expect((await post(c.headers, "accept")).json().error.code).toBe("invalid_state");
    b.push.sent = [];
    const jb = await post(c.headers, "join");
    expect(jb.statusCode).toBe(200);
    expect(jb.json().call).toMatchObject({ state: "active", device_id: a.device_id });
    expect(jb.json().join).toMatchObject({ room: call.call_id, identity: `device_${c.device_id}` });
    expect(eventsOf(b).at(-1)).toMatchObject({ type: "call.accept", by: "user", device_id: c.device_id });
    // pushed at normal priority to the other phones (not to the one that joined)
    await flush();
    expect(b.push.sent.map((s) => s.target.deviceId).sort()).toEqual([a.device_id, d.device_id].sort());
    expect(b.push.sent[0]!.opts).toMatchObject({ highPriority: false, ttlSeconds: 3600 });
    expect(JSON.parse(b.push.sent[0]!.payload.event!)).toMatchObject({ type: "call.accept", device_id: c.device_id });
    // the phone that declined joins too; joining again only renews the token
    expect((await post(d.headers, "join")).statusCode).toBe(200);
    const n = eventsOf(b).length;
    expect((await post(d.headers, "join")).statusCode).toBe(200);
    expect(eventsOf(b)).toHaveLength(n);

    // A hangs up and comes back
    expect((await post(a.headers, "end")).json().state).toBe("active");
    expect((await post(a.headers, "join")).statusCode).toBe(200);
    // everyone leaves: the last one ends the call
    await post(a.headers, "end");
    await post(c.headers, "end");
    expect((await post(d.headers, "end")).json()).toMatchObject({ state: "ended", ended_by: "user" });
    const late = await post(c.headers, "join");
    expect(late.statusCode).toBe(409);
    expect(late.json().error.code).toBe("call_ended");

    // errors: unknown call, agent credentials
    expect((await post(a.headers, "join", "call_01JB0000000000000000000000")).statusCode).toBe(404);
    expect((await post(AGENT, "join")).statusCode).toBe(403);
    const types = eventsOf(b).map((e) => e.type);
    expect(types).toEqual(["call.invite", "call.decline", "call.accept", "call.accept", "call.accept", "call.accept", "call.end"]);
  });

  it("join while the call still rings is the same as accepting, also after declining", async () => {
    const b = await app();
    const a = await pairDevice(b, "A");
    await pairDevice(b, "B", "fcm-tok-2");
    const { call } = (await placeCall(b)).json();
    await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/decline`, headers: a.headers });
    const r = await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/join`, headers: a.headers });
    expect(r.statusCode).toBe(200);
    expect(r.json().call).toMatchObject({ state: "active", device_id: a.device_id });
    expect(r.json().call.answered_at).toBeGreaterThan(0);
    expect(eventsOf(b).map((e) => e.type)).toEqual(["call.invite", "call.decline", "call.accept"]);
    // no ring timer left to fire on an answered call
    expect((await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/end`, headers: AGENT })).json().state).toBe("ended");
    const j = await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/join`, headers: a.headers });
    expect(j.json().error.code).toBe("call_ended");
  });

  it("a stale LiveKit session leaving doesn't take a phone that rejoined off the call", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const b = await app({ ringTimeoutS: 30 });
    const a = await pairDevice(b, "A");
    const { call } = (await placeCall(b)).json();
    const t0 = Date.now();
    await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/accept`, headers: a.headers });
    vi.advanceTimersByTime(60_000);
    // A drops and rejoins with the same identity; LiveKit kicks the old session
    await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/join`, headers: a.headers });
    const left = (joinedAtMs: number) =>
      b.app.inject({
        method: "POST",
        url: "/v1/livekit/webhook",
        headers: { authorization: "ok", "content-type": "application/webhook+json" },
        payload: JSON.stringify({
          event: "participant_left",
          room: { name: call.call_id },
          participant: { identity: `device_${a.device_id}`, joinedAtMs: String(joinedAtMs) },
        }),
      });
    await left(t0 + 500);
    expect(b.ctx.calls!.get(call.call_id)!.state).toBe("active");
    // the new session leaving is a real leave: last phone gone after the ring window
    await left(Date.now() + 300);
    expect(b.ctx.calls!.get(call.call_id)).toMatchObject({ state: "ended", end_reason: "room_closed" });
  });

  it("rings out to missed after the timeout and survives a restart", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const b = await app({ ringTimeoutS: 5 });
    await pairDevice(b);
    const { call } = (await placeCall(b)).json();
    vi.advanceTimersByTime(4_000);
    expect(b.ctx.calls!.get(call.call_id)!.state).toBe("ringing");
    vi.advanceTimersByTime(1_500);
    expect(b.ctx.calls!.get(call.call_id)!.state).toBe("missed");
    expect(eventsOf(b).at(-1)).toMatchObject({ type: "call.missed", call_id: call.call_id });
    expect(b.livekit.deleted).toEqual([call.call_id]);
    // A new call can be placed now.
    expect((await placeCall(b)).statusCode).toBe(201);
  });

  it("decline and cancel", async () => {
    const b = await app();
    const dev = await pairDevice(b);
    const c1 = (await placeCall(b)).json().call;
    // The callee can't "end" a ringing call; it declines.
    expect((await b.app.inject({ method: "POST", url: `/v1/calls/${c1.call_id}/end`, headers: dev.headers })).statusCode).toBe(403);
    const d = await b.app.inject({ method: "POST", url: `/v1/calls/${c1.call_id}/decline`, headers: dev.headers });
    expect(d.json()).toMatchObject({ state: "declined", ended_by: "user" });

    const c2 = (await placeCall(b)).json().call;
    const e = await b.app.inject({ method: "POST", url: `/v1/calls/${c2.call_id}/end`, headers: AGENT });
    expect(e.json()).toMatchObject({ state: "canceled", ended_by: "agent", end_reason: "canceled" });
    expect(eventsOf(b).at(-1)).toMatchObject({ type: "call.end", reason: "canceled", by: "agent" });
    // Accepting a canceled call fails.
    expect((await b.app.inject({ method: "POST", url: `/v1/calls/${c2.call_id}/accept`, headers: dev.headers })).statusCode).toBe(409);
  });

  it("validates and reports why a call can't be placed", async () => {
    const b = await app();
    expect((await placeCall(b)).json().error.code).toBe("no_devices");
    const dev = await pairDevice(b);
    expect((await placeCall(b, {})).statusCode).toBe(400);
    expect((await placeCall(b, { reason: "x", priority: "loud" })).statusCode).toBe(400);
    // the user calling needs the daemon online
    const r = await b.app.inject({ method: "POST", url: "/v1/calls", headers: dev.headers, payload: {} });
    expect(r.statusCode).toBe(503);
    expect(r.json().error.code).toBe("agent_offline");
    expect((await b.app.inject({ url: "/v1/calls/call_01JB0000000000000000000000", headers: AGENT })).statusCode).toBe(404);
    const list = (await b.app.inject({ url: "/v1/calls", headers: dev.headers })).json();
    expect(list.calls).toEqual([]);
  });

  it("is unavailable without LiveKit", async () => {
    const b = await app();
    b.ctx.livekit = undefined;
    await pairDevice(b);
    const r = await placeCall(b);
    expect(r.statusCode).toBe(503);
    expect(r.json().error.code).toBe("calls_unavailable");
  });

  it("LiveKit webhook ends an active call when the phone leaves the room", async () => {
    const b = await app();
    const dev = await pairDevice(b);
    const { call } = (await placeCall(b)).json();
    await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/accept`, headers: dev.headers });
    const hook = (authorization: string, body: object) =>
      b.app.inject({
        method: "POST",
        url: "/v1/livekit/webhook",
        headers: { authorization, "content-type": "application/webhook+json" },
        payload: JSON.stringify(body),
      });
    expect((await hook("forged", { event: "room_finished", room: { name: call.call_id } })).statusCode).toBe(401);
    // Not reachable through the public proxy, even with a valid signature.
    const proxied = await b.app.inject({
      method: "POST",
      url: "/v1/livekit/webhook",
      headers: { authorization: "ok", "content-type": "application/webhook+json", "x-forwarded-for": "203.0.113.9" },
      payload: JSON.stringify({ event: "room_finished", room: { name: call.call_id } }),
    });
    expect(proxied.statusCode).toBe(404);
    expect(b.ctx.calls!.get(call.call_id)!.state).toBe("active");
    // someone else leaving doesn't end it
    await hook("ok", { event: "participant_left", room: { name: call.call_id }, participant: { identity: "observer" } });
    expect(b.ctx.calls!.get(call.call_id)!.state).toBe("active");
    const r = await hook("ok", { event: "participant_left", room: { name: call.call_id }, participant: { identity: `device_${dev.device_id}` } });
    expect(r.statusCode).toBe(200);
    expect(b.ctx.calls!.get(call.call_id)).toMatchObject({ state: "ended", ended_by: "relay", end_reason: "room_closed" });
  });

  describe("targeted calls (device_ids)", () => {
    const pushedTo = (b: Awaited<ReturnType<typeof app>>, type: string) =>
      b.push.sent.filter((x) => JSON.parse(x.payload.event!).type === type).map((x) => x.target.deviceId);

    it("rings only the targeted phones; the others never see or touch the call", async () => {
      const b = await app();
      const a = await pairDevice(b, "A", "fcm-tok-a");
      const other = await pairDevice(b, "B", "fcm-tok-b");

      const r = await placeCall(b, { reason: "Done", device_ids: [a.device_id, a.device_id] });
      expect(r.statusCode).toBe(201);
      const { call } = r.json();
      expect(call.device_ids).toEqual([a.device_id]);
      await flush();

      // only A gets the invite, and the event names its targets
      expect(pushedTo(b, "call.invite")).toEqual([a.device_id]);
      expect(eventsOf(b).find((e) => e.type === "call.invite")).toMatchObject({ device_ids: [a.device_id] });

      const post = (h: { authorization: string }, action: string) =>
        b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/${action}`, headers: h });
      // B can't answer, decline or join, and doesn't see the call anywhere
      for (const action of ["accept", "decline", "join"]) {
        const res = await post(other.headers, action);
        expect(res.statusCode).toBe(403);
        expect(res.json().error.code).toBe("forbidden");
      }
      expect((await post(other.headers, "ringing")).statusCode).toBe(204);
      expect(eventsOf(b).filter((e) => e.type === "call.ringing")).toHaveLength(0);
      expect((await b.app.inject({ url: `/v1/calls/${call.call_id}`, headers: other.headers })).statusCode).toBe(404);
      expect((await b.app.inject({ url: "/v1/calls", headers: other.headers })).json().calls).toEqual([]);
      expect((await b.app.inject({ url: "/v1/status", headers: other.headers })).json().active_call).toBeNull();
      const evB = (await b.app.inject({ url: "/v1/events", headers: other.headers })).json();
      expect(evB.events.filter((e: { type: string }) => e.type.startsWith("call."))).toEqual([]);
      expect(evB.next_after_seq).toBe(evB.last_seq);

      // A and the agent see it
      expect((await b.app.inject({ url: "/v1/status", headers: a.headers })).json().active_call.call_id).toBe(call.call_id);
      expect((await b.app.inject({ url: "/v1/calls", headers: a.headers })).json().calls).toHaveLength(1);
      expect((await b.app.inject({ url: `/v1/calls/${call.call_id}`, headers: AGENT })).json().device_ids).toEqual([a.device_id]);

      // A answers: B gets no "join" push; the agent hangs up: only A is told
      expect((await post(a.headers, "accept")).json().call.state).toBe("active");
      expect((await post(other.headers, "join")).statusCode).toBe(403);
      expect((await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/end`, headers: AGENT })).json().state).toBe("ended");
      await flush();
      expect(pushedTo(b, "call.accept")).toEqual([]);
      expect(pushedTo(b, "call.end")).toEqual([a.device_id]);
      expect(b.push.sent.every((x) => x.target.deviceId === a.device_id)).toBe(true);
    });

    it("a targeted call is declined when its only phone declines", async () => {
      const b = await app();
      const a = await pairDevice(b, "A", "fcm-tok-a");
      await pairDevice(b, "B", "fcm-tok-b");
      const { call } = (await placeCall(b, { reason: "Done", device_ids: [a.device_id] })).json();
      const res = await b.app.inject({ method: "POST", url: `/v1/calls/${call.call_id}/decline`, headers: a.headers });
      expect(res.json().state).toBe("declined");
    });

    it("rejects empty, unknown and revoked ids, and device callers", async () => {
      const b = await app();
      const a = await pairDevice(b, "A", "fcm-tok-a");
      const gone = await pairDevice(b, "B", "fcm-tok-b");
      await b.app.inject({ method: "DELETE", url: `/v1/devices/${gone.device_id}`, headers: AGENT });
      const unknown = "dev_" + "0".repeat(26);
      for (const device_ids of [[], [unknown], [a.device_id, gone.device_id], ["not-an-id"]]) {
        const r = await placeCall(b, { reason: "Done", device_ids });
        expect(r.statusCode, JSON.stringify(device_ids)).toBe(400);
        expect(r.json().error.code).toBe("invalid_request");
      }
      b.ctx.hub.isAgentOnline = () => true;
      const fromPhone = await b.app.inject({ method: "POST", url: "/v1/calls", headers: a.headers, payload: { device_ids: [a.device_id] } });
      expect(fromPhone.statusCode).toBe(400);
      expect(eventsOf(b).filter((e) => e.type === "call.invite")).toHaveLength(0);
    });

    it("the WebSocket of a non-targeted phone gets none of the call's events, live or on replay", async () => {
      const b = await app();
      await b.app.listen({ port: 0, host: "127.0.0.1" });
      const base = `ws://127.0.0.1:${(b.app.server.address() as { port: number }).port}/v1/ws`;
      const a = await pairDevice(b, "A", "fcm-tok-a");
      const other = await pairDevice(b, "B", "fcm-tok-b");
      const open = (h: Record<string, string>) => {
        const ws = new WebSocket(base, { headers: h });
        const frames: Array<{ type: string; call_id?: string }> = [];
        ws.on("message", (m) => frames.push(JSON.parse(m.toString())));
        cleanup.push(async () => ws.close());
        return new Promise<typeof frames>((r) => ws.on("open", () => r(frames)));
      };
      const [fa, fb] = [await open(a.headers), await open(other.headers)];
      const { call } = (await placeCall(b, { reason: "Done", device_ids: [a.device_id] })).json();
      await b.app.inject({ method: "POST", url: "/v1/messages", headers: AGENT, payload: { text: "after" } });
      const until = async (pred: () => boolean) => {
        for (let i = 0; i < 100 && !pred(); i++) await new Promise((r) => setTimeout(r, 20));
      };
      await until(() => fb.some((f) => f.type === "msg.new") && fa.some((f) => f.type === "msg.new"));
      expect(fa.some((f) => f.type === "call.invite" && f.call_id === call.call_id)).toBe(true);
      expect(fb.filter((f) => f.type.startsWith("call."))).toEqual([]);

      const replay = await open(other.headers);
      await until(() => replay.some((f) => f.type === "agent.presence"));
      expect(replay.map((f) => f.type)).toEqual(["ws.hello", "msg.new", "agent.presence"]);
    });
  });
});
