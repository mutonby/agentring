import type { FastifyInstance } from "fastify";
import { randomBytes } from "node:crypto";
import { sha256 } from "../auth.js";
import { type Ctx, principal, requireAuth } from "../context.js";
import { ApiError, notFound } from "../errors.js";
import { newId, randomToken } from "../ids.js";
import type { Device, Schemas } from "../protocol.js";
import { checkPair } from "../quota.js";

const PAIR_WINDOW_MS = 60_000;
const PAIR_MAX_ATTEMPTS = 10;

interface DeviceRow {
  device_id: string;
  name: string;
  platform: string;
  app_version: string | null;
  fcm_token: string | null;
  created_at: number;
  last_seen_at: number | null;
  revoked_at: number | null;
}

export function deviceFromRow(r: DeviceRow): Device {
  return {
    device_id: r.device_id,
    name: r.name,
    platform: "android",
    ...(r.app_version ? { app_version: r.app_version } : {}),
    created_at: r.created_at,
    ...(r.last_seen_at ? { last_seen_at: r.last_seen_at } : {}),
    ...(r.revoked_at ? { revoked_at: r.revoked_at } : {}),
    has_push_token: r.fcm_token !== null,
  };
}

/** A one-time pairing token and the QR payload that carries it (also used by the hub admin API). */
export function createPairingToken(ctx: Ctx, ttlS = 600): Schemas["PairingToken"] {
  const { db } = ctx;
  const token = randomToken("pt");
  const now = Date.now();
  const expiresAt = now + ttlS * 1000;
  db.prepare("DELETE FROM pairing_tokens WHERE expires_at < ?").run(now - 86_400_000);
  db.prepare("INSERT INTO pairing_tokens (token_hash, expires_at, created_at) VALUES (?, ?, ?)").run(
    sha256(token).toString("hex"),
    expiresAt,
    now,
  );
  const qr = { v: 1, relay_url: ctx.cfg.publicUrl, pairing_token: token };
  return { pairing_token: token, expires_at: expiresAt, qr_payload: JSON.stringify(qr) };
}

export function listDevices(ctx: Ctx, includeRevoked = false): Device[] {
  const rows = ctx.db
    .prepare(`SELECT * FROM devices ${includeRevoked ? "" : "WHERE revoked_at IS NULL"} ORDER BY created_at`)
    .all() as unknown as DeviceRow[];
  return rows.map(deviceFromRow);
}

/** Revokes a device and closes its sockets. Throws 404 for an unknown device. */
export function revokeDevice(ctx: Ctx, deviceId: string): void {
  const row = ctx.db.prepare("SELECT revoked_at FROM devices WHERE device_id = ?").get(deviceId);
  if (!row) throw notFound("Device");
  ctx.db
    .prepare("UPDATE devices SET revoked_at = COALESCE(revoked_at, ?), fcm_token = NULL WHERE device_id = ?")
    .run(Date.now(), deviceId);
  ctx.hub.disconnectDevice(deviceId);
}

export function pairingRoutes(app: FastifyInstance, ctx: Ctx): void {
  const { db } = ctx;
  const attempts = new Map<string, number[]>();

  app.post<{ Body: { ttl_s?: number } | undefined }>(
    "/v1/pairing-tokens",
    {
      preHandler: requireAuth(ctx, "agent"),
      schema: {
        body: {
          type: ["object", "null"],
          additionalProperties: false,
          properties: { ttl_s: { type: "integer", minimum: 30, maximum: 600 } },
        },
      },
    },
    async (req, reply) => reply.code(201).send(createPairingToken(ctx, req.body?.ttl_s ?? 600)),
  );

  app.post<{ Body: Schemas["PairRequest"] }>(
    "/v1/pair",
    {
      schema: {
        body: {
          type: "object",
          additionalProperties: false,
          required: ["pairing_token", "device_name", "platform"],
          properties: {
            pairing_token: { type: "string", maxLength: 128 },
            fcm_token: { type: "string", maxLength: 4096 },
            device_name: { type: "string", minLength: 1, maxLength: 64 },
            platform: { type: "string", enum: ["android"] },
            app_version: { type: "string", maxLength: 32 },
          },
        },
      },
    },
    async (req, reply) => {
      const now = Date.now();
      const recent = (attempts.get(req.ip) ?? []).filter((t) => t > now - PAIR_WINDOW_MS);
      if (recent.length >= PAIR_MAX_ATTEMPTS) throw new ApiError(429, "rate_limited", "Too many pairing attempts");
      recent.push(now);
      attempts.set(req.ip, recent);

      const b = req.body;
      checkPair(db, ctx.limits());
      // Atomically consume the token.
      const res = db
        .prepare("UPDATE pairing_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?")
        .run(now, sha256(b.pairing_token).toString("hex"), now);
      if (res.changes !== 1) throw new ApiError(401, "invalid_pairing_token", "Pairing token is invalid, expired or already used");

      const deviceId = newId("dev");
      const pushSecret = randomBytes(32).toString("base64url");
      db.prepare(
        `INSERT INTO devices (device_id, name, platform, app_version, push_secret, fcm_token, created_at, last_seen_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      ).run(deviceId, b.device_name, b.platform, b.app_version ?? null, pushSecret, b.fcm_token ?? null, now, now);

      const body: Schemas["PairResponse"] = {
        device_id: deviceId,
        device_jwt: await ctx.auth.issueDeviceJwt(deviceId),
        push_secret: pushSecret,
        relay_id: ctx.relayId,
        agent_name: ctx.cfg.agentName,
        push_config: ctx.pushConfig,
      };
      req.log.info({ device: deviceId, name: b.device_name }, "device paired");
      return reply.code(201).send(body);
    },
  );

  app.post<{ Body: { fcm_token: string } }>(
    "/v1/device/fcm",
    {
      preHandler: requireAuth(ctx, "device"),
      schema: {
        body: {
          type: "object",
          additionalProperties: false,
          required: ["fcm_token"],
          properties: { fcm_token: { type: "string", minLength: 1, maxLength: 4096 } },
        },
      },
    },
    async (req, reply) => {
      const p = principal(req);
      if (p.kind !== "device") throw new Error("unreachable");
      db.prepare("UPDATE devices SET fcm_token = ? WHERE device_id = ?").run(req.body.fcm_token, p.deviceId);
      return reply.code(204).send();
    },
  );

  app.get<{ Querystring: { include_revoked?: boolean } }>(
    "/v1/devices",
    {
      preHandler: requireAuth(ctx, "agent"),
      schema: { querystring: { type: "object", properties: { include_revoked: { type: "boolean" } } } },
    },
    async (req) => ({ devices: listDevices(ctx, req.query.include_revoked ?? false) }),
  );

  app.delete<{ Params: { device_id: string } }>(
    "/v1/devices/:device_id",
    { preHandler: requireAuth(ctx, "agent") },
    async (req, reply) => {
      revokeDevice(ctx, req.params.device_id);
      req.log.info({ device: req.params.device_id }, "device revoked");
      return reply.code(204).send();
    },
  );
}
