import type { FastifyInstance } from "fastify";
import type { Principal } from "../auth.js";
import { type Ctx, principal, requireAuth } from "../context.js";
import { ApiError, forbidden, notFound } from "../errors.js";
import { newId } from "../ids.js";
import type { Event, Schemas } from "../protocol.js";
import { callMsLeft, checkCall } from "../quota.js";

type Call = Schemas["Call"];
type CallState = Schemas["CallState"];
type Direction = Schemas["CallDirection"];

interface CallRow {
  call_id: string;
  direction: Direction;
  state: CallState;
  reason: string;
  priority: "normal" | "urgent";
  agent_name: string;
  created_at: number;
  expires_at: number;
  answered_at: number | null;
  ended_at: number | null;
  ended_by: Schemas["Actor"] | null;
  end_reason: Schemas["EndReason"] | null;
  device_id: string | null;
  /** JSON array of the targeted device ids; NULL = every paired phone rings. */
  device_ids: string | null;
}

/** The phones a targeted call rings, or undefined when it rings every paired phone. */
export function targetsOf(r: Pick<CallRow, "device_ids">): string[] | undefined {
  return r.device_ids ? (JSON.parse(r.device_ids) as string[]) : undefined;
}

/** Whether `p` may see this call at all: a targeted call is private to its phones. */
export function canSee(r: Pick<CallRow, "device_ids">, p: Principal): boolean {
  if (p.kind === "agent") return true;
  const targets = targetsOf(r);
  return targets === undefined || targets.includes(p.deviceId);
}

export function callFromRow(r: CallRow): Call {
  return {
    call_id: r.call_id,
    direction: r.direction,
    state: r.state,
    ...(r.reason ? { reason: r.reason } : {}),
    priority: r.priority,
    agent_name: r.agent_name,
    created_at: r.created_at,
    ...(r.answered_at ? { answered_at: r.answered_at } : {}),
    ...(r.ended_at ? { ended_at: r.ended_at } : {}),
    ...(r.ended_by ? { ended_by: r.ended_by } : {}),
    ...(r.end_reason ? { end_reason: r.end_reason } : {}),
    ...(r.answered_at && r.ended_at ? { duration_ms: r.ended_at - r.answered_at } : {}),
    ...(r.device_id ? { device_id: r.device_id } : {}),
    ...(r.device_ids ? { device_ids: targetsOf(r) } : {}),
  };
}

/** LiveKit room of a call; hub mode prefixes it with the tenant (one LiveKit for every tenant). */
export const roomName = (ctx: Pick<Ctx, "roomPrefix">, callId: string) => ctx.roomPrefix + callId;
const identityOf = (p: Principal) => (p.kind === "agent" ? "agent" : `device_${p.deviceId}`);
/** Which side placed the call. */
const callerKind = (d: Direction) => (d === "agent_to_user" ? "agent" : "device");
const invalidState = (state: CallState) => new ApiError(409, "invalid_state", `Call is ${state}`);
const notForThisDevice = () => forbidden("This call is for other devices");

/**
 * Call state machine, ring timer and LiveKit room lifecycle.
 *
 *   ringing ─accept→ active ─end→ ended
 *   ringing ─decline→ declined | ─timeout→ missed | ─caller end→ canceled
 *
 * Group calls (agent → user): every paired phone rings and each one may answer
 * until `expires_at`; they all join the same room. While the call is active any
 * paired phone may also join it later (`join`), e.g. after its ring ended, after
 * declining or after hanging up. A phone hanging up only leaves; the call ends
 * when the agent hangs up or the last phone leaves. The call is `declined` only
 * when every ringing phone declined.
 *
 * Targeted calls (`device_ids`, agent only): only those phones ring, and the call
 * is private to them. Other phones get none of its events, don't see it in the
 * REST API and can't answer or join it (`canSee`).
 */
type PartState = "ringing" | "joined" | "declined" | "missed" | "left";
/** LiveKit reports join times in its own clock (second precision on older servers). */
const JOIN_CLOCK_SLACK_MS = 2_000;
export class Calls {
  private readonly timers = new Map<string, NodeJS.Timeout>();
  /** Devices that already reported `ringing`, so the event is emitted once per device. */
  private readonly ringingDevices = new Map<string, Set<string>>();
  /** Hub mode: ends an active call when the tenant's call minutes run out. */
  private readonly quotaTimers = new Map<string, NodeJS.Timeout>();

  constructor(private readonly ctx: Ctx) {}

  get(callId: string): CallRow | undefined {
    return this.ctx.db.prepare("SELECT * FROM calls WHERE call_id = ?").get(callId) as unknown as CallRow | undefined;
  }

  open(): CallRow | undefined {
    return this.ctx.db
      .prepare("SELECT * FROM calls WHERE state IN ('ringing','active') ORDER BY call_id DESC LIMIT 1")
      .get() as unknown as CallRow | undefined;
  }

  /** After a restart: time out calls that expired while we were down and re-arm the others. */
  resume(): void {
    const open = this.ctx.db.prepare("SELECT * FROM calls WHERE state IN ('ringing','active')").all() as unknown as CallRow[];
    for (const c of open) {
      this.armTimer(c);
      if (c.state === "active") this.armQuota(c.call_id);
    }
  }

  private part(callId: string, deviceId: string): PartState | undefined {
    return this.partRow(callId, deviceId)?.state;
  }

  private partRow(callId: string, deviceId: string): { state: PartState; updated_at: number } | undefined {
    return this.ctx.db
      .prepare("SELECT state, updated_at FROM call_participants WHERE call_id = ? AND device_id = ?")
      .get(callId, deviceId) as { state: PartState; updated_at: number } | undefined;
  }

  private setPart(callId: string, deviceId: string, state: PartState): void {
    this.ctx.db
      .prepare(
        `INSERT INTO call_participants (call_id, device_id, state, updated_at) VALUES (?, ?, ?, ?)
         ON CONFLICT(call_id, device_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at`,
      )
      .run(callId, deviceId, state, Date.now());
  }

  private countParts(callId: string, ...states: PartState[]): number {
    const r = this.ctx.db
      .prepare(`SELECT COUNT(*) AS n FROM call_participants WHERE call_id = ? AND state IN (${states.map(() => "?").join(",")})`)
      .get(callId, ...states) as { n: number };
    return r.n;
  }

  stop(): void {
    for (const t of [...this.timers.values(), ...this.quotaTimers.values()]) clearTimeout(t);
    this.timers.clear();
    this.quotaTimers.clear();
  }

  /** The call just became active: if the tenant has a call-time limit, end the call when it runs out. */
  private armQuota(callId: string): void {
    const left = callMsLeft(this.ctx.db, this.ctx.limits());
    if (left === undefined || this.quotaTimers.has(callId)) return;
    const t = setTimeout(() => {
      this.quotaTimers.delete(callId);
      // The limit may have been raised meanwhile (e.g. an upgrade): check again.
      const again = callMsLeft(this.ctx.db, this.ctx.limits());
      if (again === undefined || again >= 1000) return this.armQuota(callId);
      this.finish(callId, "active", "ended", "relay", "quota");
    }, left);
    t.unref();
    this.quotaTimers.set(callId, t);
  }

  /** Whether a device may see this event; events of a targeted call only reach its phones. */
  eventVisibleTo(event: Event, p: Principal): boolean {
    if (p.kind === "agent" || !event.type.startsWith("call.")) return true;
    const c = this.get((event as { call_id: string }).call_id);
    return !c || canSee(c, p);
  }

  create(p: Principal, body: Schemas["CreateCallRequest"]): CallRow {
    const direction: Direction = p.kind === "agent" ? "agent_to_user" : "user_to_agent";
    let targets: string[] | undefined;
    if (direction === "agent_to_user") {
      if (!body.reason) throw new ApiError(400, "invalid_request", "reason is required");
      if (body.device_ids !== undefined) targets = this.validTargets(body.device_ids);
      const n = this.ctx.db.prepare("SELECT COUNT(*) AS n FROM devices WHERE revoked_at IS NULL").get() as { n: number };
      if (n.n === 0) throw new ApiError(409, "no_devices", "No paired devices");
    } else if (body.device_ids !== undefined) {
      throw new ApiError(400, "invalid_request", "device_ids is only for calls placed by the agent");
    } else if (!this.ctx.hub.isAgentOnline()) {
      throw new ApiError(503, "agent_offline", "The agent is not connected");
    }
    if (this.open()) throw new ApiError(409, "busy", "Another call is ringing or active");
    checkCall(this.ctx.db, this.ctx.limits());

    const now = Date.now();
    const row: CallRow = {
      call_id: newId("call"),
      direction,
      state: "ringing",
      reason: body.reason ?? "",
      priority: body.priority ?? "normal",
      agent_name: (p.kind === "agent" ? body.agent_name : undefined) ?? this.ctx.cfg.agentName,
      created_at: now,
      expires_at: now + this.ctx.cfg.ringTimeoutS * 1000,
      answered_at: null,
      ended_at: null,
      ended_by: null,
      end_reason: null,
      device_id: p.kind === "device" ? p.deviceId : null,
      device_ids: targets ? JSON.stringify(targets) : null,
    };
    this.ctx.db
      .prepare(
        `INSERT INTO calls (call_id, direction, state, reason, priority, agent_name, created_at, expires_at, device_id, device_ids)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(row.call_id, direction, "ringing", row.reason, row.priority, row.agent_name, now, row.expires_at, row.device_id, row.device_ids);
    if (direction === "agent_to_user") {
      const devs =
        targets ??
        (this.ctx.db.prepare("SELECT device_id FROM devices WHERE revoked_at IS NULL").all() as Array<{ device_id: string }>).map(
          (d) => d.device_id,
        );
      for (const d of devs) this.setPart(row.call_id, d, "ringing");
    } else if (p.kind === "device") {
      this.setPart(row.call_id, p.deviceId, "joined");
    }
    this.ctx.events.emit({
      type: "call.invite",
      call_id: row.call_id,
      direction,
      agent_name: row.agent_name,
      reason: row.reason,
      priority: row.priority,
      expires_at: row.expires_at,
      ...(targets ? { device_ids: targets } : {}),
    });
    this.armTimer(row);
    return row;
  }

  /** Targeted call: every id must be a paired, non-revoked phone. Duplicates are dropped. */
  private validTargets(ids: string[]): string[] {
    const unique = [...new Set(ids)];
    if (unique.length === 0) throw new ApiError(400, "invalid_request", "device_ids must not be empty");
    const active = new Set(
      (
        this.ctx.db
          .prepare(`SELECT device_id FROM devices WHERE revoked_at IS NULL AND device_id IN (${unique.map(() => "?").join(",")})`)
          .all(...unique) as Array<{ device_id: string }>
      ).map((d) => d.device_id),
    );
    const bad = unique.filter((id) => !active.has(id));
    if (bad.length > 0) throw new ApiError(400, "invalid_request", `Unknown or revoked device_ids: ${bad.join(", ")}`);
    return unique;
  }

  ringing(callId: string, deviceId: string): void {
    const c = this.get(callId);
    if (!c) throw notFound("Call");
    const stillRinging = c.state === "ringing" || (c.state === "active" && Date.now() <= c.expires_at);
    if (!stillRinging || c.direction !== "agent_to_user" || !canSee(c, { kind: "device", deviceId })) return;
    let set = this.ringingDevices.get(callId);
    if (!set) this.ringingDevices.set(callId, (set = new Set()));
    if (set.has(deviceId)) return;
    set.add(deviceId);
    this.ctx.events.emit({ type: "call.ringing", call_id: callId, device_id: deviceId });
  }

  /**
   * Answer. For agent calls any phone may answer while the call rings (until
   * `expires_at`), and a phone that already joined may rejoin (new token) while
   * the call is active. The first answer moves the call to `active`.
   */
  accept(callId: string, p: Principal): CallRow {
    const c = this.mustBeCallee(callId, p);
    const now = Date.now();
    if (p.kind === "device") {
      const prev = this.part(callId, p.deviceId);
      if (c.state === "active" && prev === "joined") return this.rejoin(c, p.deviceId); // rejoin after a drop
      const ringing = c.state === "ringing" || (c.state === "active" && now <= c.expires_at);
      if (!ringing || (prev !== undefined && prev !== "ringing")) throw invalidState(c.state === "active" ? "active" : c.state);
      return this.joinDevice(c, p.deviceId);
    }
    const res = this.ctx.db
      .prepare("UPDATE calls SET state = 'active', answered_at = ? WHERE call_id = ? AND state = 'ringing'")
      .run(now, callId);
    if (res.changes !== 1) throw invalidState(this.get(callId)!.state);
    this.settle(callId);
    this.armQuota(callId);
    this.ctx.events.emit({ type: "call.accept", call_id: callId, by: "agent" });
    return this.get(callId)!;
  }

  /**
   * "Join the call": any paired phone that is not on an agent call may join it
   * while it is ringing or active, also after its ring window, after declining or
   * after hanging up. While the call rings this is the same as accepting.
   */
  join(callId: string, deviceId: string): CallRow {
    const c = this.get(callId);
    if (!c) throw notFound("Call");
    if (c.direction !== "agent_to_user") throw forbidden("Only calls from the agent can be joined");
    if (!canSee(c, { kind: "device", deviceId })) throw notForThisDevice();
    if (c.state !== "ringing" && c.state !== "active") throw new ApiError(409, "call_ended", `Call is ${c.state}`);
    if (c.state === "active" && this.part(callId, deviceId) === "joined") return this.rejoin(c, deviceId);
    return this.joinDevice(c, deviceId);
  }

  /** A phone already on the call asks for a new token: no event, but its join time moves on. */
  private rejoin(c: CallRow, deviceId: string): CallRow {
    this.setPart(c.call_id, deviceId, "joined");
    return c;
  }

  /** A phone joins the room; the first one moves a ringing call to `active`. Emits `call.accept`. */
  private joinDevice(c: CallRow, deviceId: string): CallRow {
    if (c.state === "ringing") {
      this.ctx.db
        .prepare("UPDATE calls SET state = 'active', answered_at = ?, device_id = ? WHERE call_id = ? AND state = 'ringing'")
        .run(Date.now(), deviceId, c.call_id);
      this.armQuota(c.call_id);
    }
    this.setPart(c.call_id, deviceId, "joined");
    this.ringingDevices.get(c.call_id)?.delete(deviceId);
    this.ctx.events.emit({ type: "call.accept", call_id: c.call_id, by: "user", device_id: deviceId });
    return this.get(c.call_id)!;
  }

  /**
   * Decline. A phone declining only stops its own ringing; the call becomes
   * `declined` when no phone is left ringing or on the call.
   */
  decline(callId: string, p: Principal): CallRow {
    const c = this.mustBeCallee(callId, p);
    const by = p.kind === "agent" ? "agent" : "user";
    if (p.kind === "device") {
      if (this.part(callId, p.deviceId) !== "ringing" || (c.state !== "ringing" && c.state !== "active")) {
        throw invalidState(c.state);
      }
      this.setPart(callId, p.deviceId, "declined");
      this.ctx.events.emit({ type: "call.decline", call_id: callId, by, device_id: p.deviceId });
      if (c.state === "ringing" && this.countParts(callId, "ringing", "joined") === 0) {
        this.ctx.db
          .prepare("UPDATE calls SET state = 'declined', ended_at = ?, ended_by = ? WHERE call_id = ? AND state = 'ringing'")
          .run(Date.now(), by, callId);
        this.settle(callId);
        this.closeRoom(callId);
      }
      return this.get(callId)!;
    }
    const res = this.ctx.db
      .prepare("UPDATE calls SET state = 'declined', ended_at = ?, ended_by = ? WHERE call_id = ? AND state = 'ringing'")
      .run(Date.now(), by, callId);
    if (res.changes !== 1) throw invalidState(this.get(callId)!.state);
    this.settle(callId);
    this.ctx.events.emit({ type: "call.decline", call_id: callId, by });
    this.closeRoom(callId);
    return this.get(callId)!;
  }

  /**
   * Hang up. While ringing only the caller may (→ canceled). On an active agent
   * call a phone only leaves; the call ends with the agent or the last phone.
   * Idempotent on terminal states.
   */
  end(callId: string, p: Principal): CallRow {
    const c = this.get(callId);
    if (!c) throw notFound("Call");
    const by = p.kind === "agent" ? "agent" : "user";
    if (c.state === "ringing") {
      if (p.kind !== callerKind(c.direction)) throw forbidden("Decline a ringing call instead of ending it");
      return this.finish(callId, "ringing", "canceled", by, "canceled") ?? this.get(callId)!;
    }
    if (c.state !== "active") return c;
    if (p.kind === "device" && c.direction === "agent_to_user") {
      const prev = this.part(callId, p.deviceId);
      if (prev === "ringing") return this.decline(callId, p);
      if (prev !== "joined") throw forbidden("This device is not on the call");
      return this.leave(callId, p.deviceId, by, "hangup");
    }
    return this.finish(callId, "active", "ended", by, "hangup") ?? this.get(callId)!;
  }

  /**
   * A phone left the room (LiveKit webhook): same as hanging up. `sessionJoinedAt`
   * is when that LiveKit session joined; a session older than the phone's latest
   * join is a stale one (e.g. kicked by its own rejoin with the same identity) and
   * must not take the phone off the call.
   */
  deviceLeft(callId: string, deviceId: string, sessionJoinedAt?: number): void {
    const p = this.partRow(callId, deviceId);
    if (p?.state !== "joined") return;
    if (sessionJoinedAt !== undefined && sessionJoinedAt < p.updated_at - JOIN_CLOCK_SLACK_MS) return;
    this.leave(callId, deviceId, "relay", "room_closed");
  }

  /** The room went away, or the agent left it (LiveKit webhook). */
  roomClosed(callId: string): void {
    this.finish(callId, "active", "ended", "relay", "room_closed");
  }

  private leave(callId: string, deviceId: string, by: Schemas["Actor"], reason: Schemas["EndReason"]): CallRow {
    this.setPart(callId, deviceId, "left");
    const c = this.get(callId)!;
    // Others may still answer until the ring window closes; then the timer ends an empty call.
    if (this.countParts(callId, "joined") === 0 && (Date.now() > c.expires_at || this.countParts(callId, "ringing") === 0)) {
      return this.finish(callId, "active", "ended", by, reason) ?? this.get(callId)!;
    }
    return c;
  }

  private finish(
    callId: string,
    from: "ringing" | "active",
    to: CallState,
    by: Schemas["Actor"],
    reason: Schemas["EndReason"],
  ): CallRow | undefined {
    const now = Date.now();
    const res = this.ctx.db
      .prepare("UPDATE calls SET state = ?, ended_at = ?, ended_by = ?, end_reason = ? WHERE call_id = ? AND state = ?")
      .run(to, now, by, reason, callId, from);
    if (res.changes !== 1) return undefined;
    this.settle(callId);
    clearTimeout(this.quotaTimers.get(callId));
    this.quotaTimers.delete(callId);
    const row = this.get(callId)!;
    this.ctx.events.emit({
      type: "call.end",
      call_id: callId,
      by,
      reason,
      ...(row.answered_at ? { duration_ms: now - row.answered_at } : {}),
    });
    this.closeRoom(callId);
    return row;
  }

  private mustBeCallee(callId: string, p: Principal): CallRow {
    const c = this.get(callId);
    if (!c) throw notFound("Call");
    if (p.kind === callerKind(c.direction)) throw forbidden("The caller cannot answer its own call");
    if (!canSee(c, p)) throw notForThisDevice();
    return c;
  }

  private armTimer(c: CallRow): void {
    const t = setTimeout(() => this.timeout(c.call_id), Math.max(0, c.expires_at - Date.now()));
    t.unref();
    this.timers.set(c.call_id, t);
  }

  private timeout(callId: string): void {
    this.timers.delete(callId);
    this.ringingDevices.delete(callId);
    // Phones that never answered stop ringing (they time out locally too).
    this.ctx.db
      .prepare("UPDATE call_participants SET state = 'missed', updated_at = ? WHERE call_id = ? AND state = 'ringing'")
      .run(Date.now(), callId);
    const c = this.get(callId);
    if (c?.state === "active") {
      // Everyone who answered already left while others could still join.
      if (c.direction === "agent_to_user" && this.countParts(callId, "joined") === 0) {
        this.finish(callId, "active", "ended", "relay", "hangup");
      }
      return;
    }
    const res = this.ctx.db
      .prepare("UPDATE calls SET state = 'missed', ended_at = ?, ended_by = 'relay' WHERE call_id = ? AND state = 'ringing'")
      .run(Date.now(), callId);
    if (res.changes !== 1) return;
    this.ctx.events.emit({ type: "call.missed", call_id: callId });
    this.closeRoom(callId);
  }

  /** The call left `ringing`: stop the timer. */
  private settle(callId: string): void {
    clearTimeout(this.timers.get(callId));
    this.timers.delete(callId);
    this.ringingDevices.delete(callId);
  }

  private closeRoom(callId: string): void {
    const lk = this.ctx.livekit;
    if (!lk) return;
    lk.deleteRoom(roomName(this.ctx, callId)).catch(() => undefined);
  }
}

/** When a LiveKit participant session joined, in ms, if the webhook says. */
function sessionJoinedAt(p: { joinedAt?: unknown; joinedAtMs?: unknown } | undefined): number | undefined {
  const ms = Number(p?.joinedAtMs ?? 0);
  if (ms > 0) return ms;
  const s = Number(p?.joinedAt ?? 0);
  return s > 0 ? s * 1000 : undefined;
}

const CALL_ID = { type: "object", properties: { call_id: { type: "string", pattern: "^call_[0-9A-HJKMNP-TV-Z]{26}$" } } } as const;

export function callRoutes(app: FastifyInstance, ctx: Ctx, calls: Calls): void {
  const auth = requireAuth(ctx, "agent", "device");
  const livekit = () => {
    if (!ctx.livekit) throw new ApiError(503, "calls_unavailable", "Calls are not configured on this relay (LiveKit is missing)");
    return ctx.livekit;
  };
  const joinFor = (callId: string, p: Principal) =>
    livekit().join(roomName(ctx, callId), identityOf(p), p.kind === "agent" ? ctx.cfg.agentName : "user");

  app.post<{ Body: Schemas["CreateCallRequest"] }>(
    "/v1/calls",
    {
      preHandler: auth,
      schema: {
        body: {
          type: "object",
          additionalProperties: false,
          properties: {
            reason: { type: "string", maxLength: 500 },
            priority: { type: "string", enum: ["normal", "urgent"] },
            agent_name: { type: "string", minLength: 1, maxLength: 64 },
            device_ids: {
              type: "array",
              minItems: 1,
              maxItems: 50,
              items: { type: "string", pattern: "^dev_[0-9A-HJKMNP-TV-Z]{26}$" },
            },
          },
        },
      },
    },
    async (req, reply) => {
      const p = principal(req);
      const lk = livekit();
      const row = calls.create(p, req.body ?? {});
      // The push is already on its way; create the room and the caller's token meanwhile.
      const [, join] = await Promise.all([
        lk.createRoom(roomName(ctx, row.call_id)).catch((err: unknown) => req.log.warn({ err }, "could not pre-create room")),
        joinFor(row.call_id, p),
      ]);
      req.log.info({ call: row.call_id, direction: row.direction, priority: row.priority }, "call created");
      return reply.code(201).send({ call: callFromRow(row), join });
    },
  );

  app.get<{ Querystring: { limit?: number; before?: string } }>(
    "/v1/calls",
    {
      preHandler: auth,
      schema: {
        querystring: {
          type: "object",
          properties: {
            limit: { type: "integer", minimum: 1, maximum: 500, default: 100 },
            before: { type: "string", maxLength: 64 },
          },
        },
      },
    },
    async (req) => {
      const { limit = 100, before } = req.query;
      const p = principal(req);
      // Devices don't see targeted calls that are not for them.
      const where = [
        ...(before ? ["call_id < ?"] : []),
        ...(p.kind === "device" ? ["(device_ids IS NULL OR EXISTS (SELECT 1 FROM json_each(calls.device_ids) WHERE value = ?))"] : []),
      ];
      const args = [...(before ? [before] : []), ...(p.kind === "device" ? [p.deviceId] : []), limit];
      const rows = ctx.db
        .prepare(`SELECT * FROM calls ${where.length ? `WHERE ${where.join(" AND ")}` : ""} ORDER BY call_id DESC LIMIT ?`)
        .all(...args) as unknown as CallRow[];
      return { calls: rows.map(callFromRow) };
    },
  );

  app.get<{ Params: { call_id: string } }>("/v1/calls/:call_id", { preHandler: auth, schema: { params: CALL_ID } }, async (req) => {
    const c = calls.get(req.params.call_id);
    if (!c || !canSee(c, principal(req))) throw notFound("Call");
    return callFromRow(c);
  });

  app.post<{ Params: { call_id: string } }>(
    "/v1/calls/:call_id/ringing",
    { preHandler: requireAuth(ctx, "device"), schema: { params: CALL_ID } },
    async (req, reply) => {
      const p = principal(req);
      if (p.kind !== "device") throw new Error("unreachable");
      calls.ringing(req.params.call_id, p.deviceId);
      return reply.code(204).send();
    },
  );

  app.post<{ Params: { call_id: string } }>(
    "/v1/calls/:call_id/accept",
    { preHandler: auth, schema: { params: CALL_ID } },
    async (req) => {
      const p = principal(req);
      livekit();
      const row = calls.accept(req.params.call_id, p);
      return { call: callFromRow(row), join: await joinFor(row.call_id, p) };
    },
  );

  app.post<{ Params: { call_id: string } }>(
    "/v1/calls/:call_id/join",
    { preHandler: requireAuth(ctx, "device"), schema: { params: CALL_ID } },
    async (req) => {
      const p = principal(req);
      if (p.kind !== "device") throw new Error("unreachable");
      livekit();
      const row = calls.join(req.params.call_id, p.deviceId);
      return { call: callFromRow(row), join: await joinFor(row.call_id, p) };
    },
  );

  app.post<{ Params: { call_id: string } }>(
    "/v1/calls/:call_id/decline",
    { preHandler: auth, schema: { params: CALL_ID } },
    async (req) => callFromRow(calls.decline(req.params.call_id, principal(req))),
  );

  app.post<{ Params: { call_id: string } }>(
    "/v1/calls/:call_id/end",
    { preHandler: auth, schema: { params: CALL_ID } },
    async (req) => callFromRow(calls.end(req.params.call_id, principal(req))),
  );

  // LiveKit webhooks: the room closing (or the callee dropping out) ends an active call.
  app.register(async (scope) => {
    scope.addContentTypeParser("application/webhook+json", { parseAs: "string" }, (_req, body, done) => done(null, body));
    scope.addContentTypeParser("application/json", { parseAs: "string" }, (_req, body, done) => done(null, body));
    scope.post<{ Body: string }>("/v1/livekit/webhook", async (req, reply) => {
      // LiveKit calls this over the internal network. Anything that came through the
      // public reverse proxy (which adds X-Forwarded-For) is turned away, signature or not.
      if (req.headers["x-forwarded-for"] !== undefined) throw notFound("Route");
      let event;
      try {
        event = await livekit().receiveWebhook(req.body, req.headers.authorization);
      } catch {
        throw new ApiError(401, "unauthorized", "Bad webhook signature");
      }
      const room = event.room?.name;
      const c = room?.startsWith(ctx.roomPrefix) ? calls.get(room.slice(ctx.roomPrefix.length)) : undefined;
      if (c?.state === "active") {
        const identity = event.participant?.identity ?? "";
        const left = event.event === "participant_left";
        if (event.event === "room_finished" || (left && identity === "agent")) {
          req.log.info({ call: c.call_id, event: event.event, identity }, "call ended by LiveKit");
          calls.roomClosed(c.call_id);
        } else if (left && identity.startsWith("device_")) {
          const deviceId = identity.slice("device_".length);
          if (c.direction === "agent_to_user") calls.deviceLeft(c.call_id, deviceId, sessionJoinedAt(event.participant));
          else if (deviceId === c.device_id) calls.roomClosed(c.call_id);
        }
      }
      return reply.code(200).send({ ok: true });
    });
  });
}
