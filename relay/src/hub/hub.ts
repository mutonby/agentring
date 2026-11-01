/**
 * Hub mode: many isolated tenants in one relay process (docs/cloud.md).
 *
 * Each tenant is a full relay (buildApp) with its own SQLite file, relay id, JWT
 * secret, API key, event log and calls, mounted under `/t/{tenant_id}`. The hub
 * strips that prefix and hands the raw request (or WebSocket upgrade) to the
 * tenant's Fastify instance, so the relay API is exactly the same as self-hosted.
 * FCM and LiveKit are shared; LiveKit rooms are prefixed with the tenant id.
 *
 * The hub's own Fastify app serves `/healthz`, the admin API (`/admin/v1/*`, for
 * the control plane) and the shared LiveKit webhook.
 */
import { createHash, timingSafeEqual } from "node:crypto";
import { existsSync, rmSync } from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { Duplex } from "node:stream";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import Fastify, { type FastifyBaseLogger, type FastifyInstance, type FastifyRequest } from "fastify";
import { buildApp, type BuiltApp } from "../app.js";
import { sha256 } from "../auth.js";
import type { Config } from "../config.js";
import { ApiError, notFound, unauthorized } from "../errors.js";
import { newId, randomToken } from "../ids.js";
import { LiveKitServer, type LiveKitService } from "../livekit.js";
import { FcmProvider } from "../push/fcm.js";
import type { PushProvider } from "../push/push-provider.js";
import { type Limits, UNLIMITED, usage } from "../quota.js";
import { createPairingToken, listDevices, revokeDevice } from "../routes/pairing.js";
import { VERSION } from "../routes/system.js";
import { Registry, type TenantRecord } from "./registry.js";

export const TENANT_ID = /^tnt_[0-9A-HJKMNP-TV-Z]{26}$/;
const TENANT_PATH = /^\/t\/(tnt_[0-9A-HJKMNP-TV-Z]{26})(\/[^?]*)?(\?.*)?$/;
/** Unload a tenant that has had no request, socket or open call for this long. */
const IDLE_UNLOAD_MS = 15 * 60_000;
const SWEEP_EVERY_MS = 60_000;
/** Agent tokens (admin API): default and maximum lifetime. */
const AGENT_TOKEN_TTL_S = 600;
const AGENT_TOKEN_MAX_TTL_S = 3600;

export interface HubOptions {
  adminToken: string;
  logger?: boolean;
  pushProvider?: PushProvider;
  livekit?: LiveKitService;
  idleUnloadMs?: number;
}

interface Tenant {
  record: TenantRecord;
  built: BuiltApp;
  lastUsed: number;
}

const LIMIT = { type: ["integer", "null"], minimum: 0 } as const;
const LIMITS_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: { max_devices: LIMIT, call_seconds_per_month: LIMIT, messages_per_day: LIMIT },
} as const;
const AGENT_NAME = { type: "string", minLength: 1, maxLength: 64 } as const;
const TID_PARAMS = {
  type: "object",
  properties: { tid: { type: "string", pattern: TENANT_ID.source } },
} as const;

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  if (res.headersSent) {
    res.end();
    return;
  }
  res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(body));
}

const errorBody = (code: string, message: string) => ({ error: { code, message } });

function rejectUpgrade(socket: Duplex, status: number, code: string, message: string): void {
  const body = JSON.stringify(errorBody(code, message));
  socket.end(
    `HTTP/1.1 ${status} ${code}\r\ncontent-type: application/json\r\ncontent-length: ${Buffer.byteLength(body)}\r\nconnection: close\r\n\r\n${body}`,
  );
}

export class Hub {
  readonly registry: Registry;
  private readonly loaded = new Map<string, Promise<Tenant | undefined>>();
  /**
   * Short-lived agent tokens by SHA-256 (hex). Kept in memory only: a restart revokes
   * them, like rotating the tenant's key or deleting the tenant does.
   */
  private readonly agentTokens = new Map<string, { tenantId: string; expiresAt: number }>();
  private readonly provider?: PushProvider;
  private readonly livekit?: LiveKitService;
  private sweeper?: NodeJS.Timeout;
  log!: FastifyBaseLogger;

  constructor(
    readonly cfg: Config,
    private readonly opts: HubOptions,
  ) {
    this.registry = new Registry(cfg.dataDir === ":memory:" ? ":memory:" : join(cfg.dataDir, "hub.db"));
    this.provider = opts.pushProvider ?? (cfg.fcm ? new FcmProvider(cfg.fcm.serviceAccountFile, cfg.fcm.apiBase) : undefined);
    this.livekit = opts.livekit ?? (cfg.livekit ? new LiveKitServer(cfg.livekit) : undefined);
  }

  baseUrl(tenantId: string): string {
    return `${this.cfg.publicUrl}/t/${tenantId}`;
  }

  private tenantDir(tenantId: string): string {
    return join(this.cfg.dataDir, "tenants", tenantId);
  }

  /** The tenant, loading it on first use. Undefined if it doesn't exist. */
  tenant(tenantId: string): Promise<Tenant | undefined> {
    let p = this.loaded.get(tenantId);
    if (!p) {
      p = this.load(tenantId);
      this.loaded.set(tenantId, p);
      p.then(
        (t) => t || this.loaded.delete(tenantId),
        () => this.loaded.delete(tenantId),
      );
    }
    return p.then((t) => {
      if (t) t.lastUsed = Date.now();
      return t;
    });
  }

  private async load(tenantId: string): Promise<Tenant | undefined> {
    const record = this.registry.get(tenantId);
    if (!record) return undefined;
    const cfg: Config = {
      ...this.cfg,
      publicUrl: this.baseUrl(tenantId),
      dataDir: this.tenantDir(tenantId),
      agentName: record.agent_name,
      apiKey: undefined,
    };
    const t = { record, lastUsed: Date.now() } as Tenant;
    t.built = await buildApp(cfg, {
      loggerInstance: this.log.child({ tenant: tenantId }),
      pushProvider: this.provider,
      livekit: this.livekit,
      tenant: {
        apiKeyHash: () => t.record.api_key_hash,
        limits: () => t.record.limits,
        agentToken: (hash) => this.agentTokenValid(tenantId, hash),
        roomPrefix: `${tenantId}.`,
      },
    });
    await t.built.app.ready();
    this.log.info({ tenant: tenantId }, "tenant loaded");
    return t;
  }

  private async unload(tenantId: string): Promise<void> {
    const p = this.loaded.get(tenantId);
    if (!p) return;
    this.loaded.delete(tenantId);
    const t = await p.catch(() => undefined);
    await t?.built.app.close();
  }

  /** Unloads tenants that are idle: no recent request, no open socket, no open call. */
  async sweep(now = Date.now()): Promise<void> {
    const idleMs = this.opts.idleUnloadMs ?? IDLE_UNLOAD_MS;
    this.pruneAgentTokens(now);
    for (const [id, p] of [...this.loaded]) {
      const t = await p.catch(() => undefined);
      if (!t) continue;
      const { ctx } = t.built;
      if (now - t.lastUsed > idleMs && ctx.hub.socketCount() === 0 && !ctx.calls?.open()) {
        await this.unload(id);
        this.log.info({ tenant: id }, "tenant unloaded (idle)");
      }
    }
  }

  /** After a restart, load the tenants with a ringing or active call so their timers run. */
  async resumeOpenCalls(): Promise<void> {
    for (const id of this.registry.ids()) {
      const file = join(this.tenantDir(id), "relay.db");
      if (!existsSync(file)) continue;
      let open = false;
      try {
        const db = new DatabaseSync(file);
        try {
          open = db.prepare("SELECT 1 FROM calls WHERE state IN ('ringing','active') LIMIT 1").get() !== undefined;
        } finally {
          db.close();
        }
      } catch (err) {
        this.log.warn({ err, tenant: id }, "could not check tenant for open calls");
      }
      if (open) await this.tenant(id);
    }
  }

  view(t: Tenant) {
    return {
      tenant_id: t.record.tenant_id,
      base_url: this.baseUrl(t.record.tenant_id),
      agent_name: t.record.agent_name,
      limits: t.record.limits,
      disabled: t.record.disabled,
      created_at: t.record.created_at,
      usage: usage(t.built.ctx.db),
    };
  }

  async create(agentName: string, limits: Limits): Promise<{ tenant: ReturnType<Hub["view"]>; api_key: string }> {
    const id = newId("tnt");
    const { apiKey } = this.registry.create(id, agentName, limits);
    const t = (await this.tenant(id))!;
    return { tenant: this.view(t), api_key: apiKey };
  }

  async update(t: Tenant, patch: { agent_name?: string; limits?: Partial<Limits>; disabled?: boolean }): Promise<void> {
    const limits = patch.limits ? { ...t.record.limits, ...patch.limits } : undefined;
    this.registry.update(t.record.tenant_id, { agent_name: patch.agent_name, limits, disabled: patch.disabled });
    t.record = this.registry.get(t.record.tenant_id)!;
    if (limits) t.built.ctx.calls?.limitsChanged();
    if (patch.agent_name !== undefined) t.built.ctx.cfg.agentName = patch.agent_name;
    // Turned off: drop every socket now; reconnects get 403 tenant_disabled.
    if (patch.disabled) t.built.ctx.hub.closeAll();
  }

  /**
   * A short-lived credential that acts as the tenant's agent, like its API key, for
   * `ttlS` seconds: lets the control plane (which never keeps tenant keys) act for a
   * tenant, e.g. a "send a test message" button, through the relay API or `mcp-http`.
   */
  issueAgentToken(tenantId: string, ttlS = AGENT_TOKEN_TTL_S): { agent_token: string; expires_at: number } {
    this.pruneAgentTokens();
    const token = randomToken("at");
    const expiresAt = Date.now() + Math.min(ttlS, AGENT_TOKEN_MAX_TTL_S) * 1000;
    this.agentTokens.set(sha256(token).toString("hex"), { tenantId, expiresAt });
    return { agent_token: token, expires_at: expiresAt };
  }

  /** The tenant an agent token belongs to, while it is valid. */
  agentTokenTenant(token: string): string | undefined {
    const e = this.agentTokens.get(sha256(token).toString("hex"));
    return e && e.expiresAt > Date.now() ? e.tenantId : undefined;
  }

  private agentTokenValid(tenantId: string, hash: Buffer): boolean {
    const e = this.agentTokens.get(hash.toString("hex"));
    return e !== undefined && e.tenantId === tenantId && e.expiresAt > Date.now();
  }

  private revokeAgentTokens(tenantId: string): void {
    for (const [h, e] of this.agentTokens) if (e.tenantId === tenantId) this.agentTokens.delete(h);
  }

  private pruneAgentTokens(now = Date.now()): void {
    for (const [h, e] of this.agentTokens) if (e.expiresAt <= now) this.agentTokens.delete(h);
  }

  rotateKey(t: Tenant): string {
    const apiKey = this.registry.rotateKey(t.record.tenant_id);
    t.record = this.registry.get(t.record.tenant_id)!;
    this.revokeAgentTokens(t.record.tenant_id);
    t.built.ctx.hub.disconnectAgent();
    return apiKey;
  }

  async delete(tenantId: string): Promise<void> {
    // Registry first: from here on no request can (re)load the tenant while it closes.
    this.registry.delete(tenantId);
    this.revokeAgentTokens(tenantId);
    await this.unload(tenantId);
    if (this.cfg.dataDir !== ":memory:") rmSync(this.tenantDir(tenantId), { recursive: true, force: true });
    this.log.info({ tenant: tenantId }, "tenant deleted");
  }

  /** Resolves `/t/{tid}/...` to a loaded, enabled tenant, or answers the error itself. */
  private async route(url: string | undefined): Promise<{ t: Tenant; rest: string } | { status: number; code: string; message: string } | null> {
    const m = url?.match(TENANT_PATH);
    if (!m) return null;
    const record = this.registry.get(m[1]!);
    if (!record) return { status: 404, code: "not_found", message: "Tenant not found" };
    if (record.disabled) return { status: 403, code: "tenant_disabled", message: "This account is disabled" };
    const t = await this.tenant(m[1]!);
    if (!t) return { status: 404, code: "not_found", message: "Tenant not found" };
    return { t, rest: (m[2] || "/") + (m[3] ?? "") };
  }

  async handleRequest(req: IncomingMessage, res: ServerResponse, fallback: (req: IncomingMessage, res: ServerResponse) => void) {
    let r;
    try {
      r = await this.route(req.url);
    } catch (err) {
      this.log.error({ err }, "tenant routing failed");
      return sendJson(res, 500, errorBody("internal", "Internal error"));
    }
    if (r === null) return fallback(req, res);
    if ("status" in r) return sendJson(res, r.status, errorBody(r.code, r.message));
    req.url = r.rest;
    r.t.built.app.routing(req, res);
  }

  async handleUpgrade(req: IncomingMessage, socket: Duplex, head: Buffer): Promise<void> {
    let r;
    try {
      r = await this.route(req.url);
    } catch (err) {
      this.log.error({ err }, "tenant routing failed");
      return rejectUpgrade(socket, 500, "internal", "Internal error");
    }
    if (r === null) return rejectUpgrade(socket, 404, "not_found", "Not found");
    if ("status" in r) return rejectUpgrade(socket, r.status, r.code, r.message);
    req.url = r.rest;
    r.t.built.app.server.emit("upgrade", req, socket, head);
  }

  startSweeper(): void {
    this.sweeper = setInterval(() => void this.sweep().catch((err: unknown) => this.log.warn({ err }, "sweep failed")), SWEEP_EVERY_MS);
    this.sweeper.unref();
  }

  async close(): Promise<void> {
    clearInterval(this.sweeper);
    await Promise.all([...this.loaded.keys()].map((id) => this.unload(id)));
    this.registry.close();
  }
}

const hashToken = (s: string) => createHash("sha256").update(s).digest();

/** Builds the hub: its Fastify app (admin API, webhook, health) wrapped around the tenant router. */
export async function buildHub(cfg: Config, opts: HubOptions): Promise<{ app: FastifyInstance; hub: Hub }> {
  if (opts.adminToken.length < 32) throw new Error("The hub admin token must be at least 32 characters");
  const hub = new Hub(cfg, opts);
  const adminHash = hashToken(opts.adminToken);

  const app = Fastify({
    logger: opts.logger ?? true,
    trustProxy: true,
    bodyLimit: 64 * 1024,
    serverFactory: (handler) => {
      const server = createServer((req, res) => void hub.handleRequest(req, res, handler));
      server.on("upgrade", (req, socket, head) => void hub.handleUpgrade(req, socket, head));
      return server;
    },
  });
  hub.log = app.log;

  app.setErrorHandler((err, req, reply) => {
    if (err instanceof ApiError) return reply.code(err.statusCode).send(errorBody(err.code, err.message));
    const e = err as { validation?: unknown; statusCode?: number; message: string };
    if (e.validation || (e.statusCode && e.statusCode < 500)) {
      return reply.code(e.statusCode ?? 400).send(errorBody("invalid_request", e.message));
    }
    req.log.error({ err }, "unhandled error");
    return reply.code(500).send(errorBody("internal", "Internal error"));
  });
  app.setNotFoundHandler((req, reply) => reply.code(404).send(errorBody("not_found", `${req.method} ${req.url} not found`)));

  app.get("/healthz", async () => ({ ok: true, hub: true, version: VERSION }));

  // ---------- admin API (control plane, internal network only) ----------
  const admin = async (req: FastifyRequest) => {
    // Like the LiveKit webhook: anything that came through the public proxy is turned away.
    if (req.headers["x-forwarded-for"] !== undefined) throw notFound("Route");
    const m = req.headers.authorization?.match(/^Bearer\s+(\S+)$/i);
    if (!m || !timingSafeEqual(hashToken(m[1]!), adminHash)) throw unauthorized();
  };
  const mustTenant = async (tid: string) => {
    const t = await hub.tenant(tid);
    if (!t) throw notFound("Tenant");
    return t;
  };

  app.register(async (scope) => {
    scope.addHook("preHandler", admin);

    scope.post<{ Body: { agent_name?: string; limits?: Partial<Limits> } | undefined }>(
      "/admin/v1/tenants",
      {
        schema: {
          body: {
            type: ["object", "null"],
            additionalProperties: false,
            properties: { agent_name: AGENT_NAME, limits: LIMITS_SCHEMA },
          },
        },
      },
      async (req, reply) => {
        const body = req.body ?? {};
        const created = await hub.create(body.agent_name ?? cfg.agentName, { ...UNLIMITED, ...body.limits });
        req.log.info({ tenant: created.tenant.tenant_id }, "tenant created");
        return reply.code(201).send(created);
      },
    );

    scope.get<{ Params: { tid: string } }>("/admin/v1/tenants/:tid", { schema: { params: TID_PARAMS } }, async (req) =>
      hub.view(await mustTenant(req.params.tid)),
    );

    scope.patch<{ Params: { tid: string }; Body: { agent_name?: string; limits?: Partial<Limits>; disabled?: boolean } }>(
      "/admin/v1/tenants/:tid",
      {
        schema: {
          params: TID_PARAMS,
          body: {
            type: "object",
            additionalProperties: false,
            properties: { agent_name: AGENT_NAME, limits: LIMITS_SCHEMA, disabled: { type: "boolean" } },
          },
        },
      },
      async (req) => {
        const t = await mustTenant(req.params.tid);
        await hub.update(t, req.body);
        return hub.view(t);
      },
    );

    scope.post<{ Params: { tid: string } }>("/admin/v1/tenants/:tid/api-key", { schema: { params: TID_PARAMS } }, async (req) => {
      const t = await mustTenant(req.params.tid);
      req.log.info({ tenant: t.record.tenant_id }, "tenant API key rotated");
      return { api_key: hub.rotateKey(t) };
    });

    scope.delete<{ Params: { tid: string } }>("/admin/v1/tenants/:tid", { schema: { params: TID_PARAMS } }, async (req, reply) => {
      await mustTenant(req.params.tid);
      await hub.delete(req.params.tid);
      return reply.code(204).send();
    });

    scope.post<{ Params: { tid: string }; Body: { ttl_s?: number } | undefined }>(
      "/admin/v1/tenants/:tid/pairing-tokens",
      {
        schema: {
          params: TID_PARAMS,
          body: {
            type: ["object", "null"],
            additionalProperties: false,
            properties: { ttl_s: { type: "integer", minimum: 30, maximum: 600 } },
          },
        },
      },
      async (req, reply) => {
        const t = await mustTenant(req.params.tid);
        return reply.code(201).send(createPairingToken(t.built.ctx, req.body?.ttl_s ?? 600));
      },
    );

    scope.post<{ Params: { tid: string }; Body: { ttl_s?: number } | undefined }>(
      "/admin/v1/tenants/:tid/agent-tokens",
      {
        schema: {
          params: TID_PARAMS,
          body: {
            type: ["object", "null"],
            additionalProperties: false,
            properties: { ttl_s: { type: "integer", minimum: 30, maximum: AGENT_TOKEN_MAX_TTL_S } },
          },
        },
      },
      async (req, reply) => {
        const t = await mustTenant(req.params.tid);
        req.log.info({ tenant: t.record.tenant_id }, "agent token issued");
        return reply.code(201).send(hub.issueAgentToken(t.record.tenant_id, req.body?.ttl_s ?? AGENT_TOKEN_TTL_S));
      },
    );

    scope.get<{ Params: { tid: string } }>("/admin/v1/tenants/:tid/devices", { schema: { params: TID_PARAMS } }, async (req) => ({
      devices: listDevices((await mustTenant(req.params.tid)).built.ctx),
    }));

    scope.delete<{ Params: { tid: string; device_id: string } }>(
      "/admin/v1/tenants/:tid/devices/:device_id",
      { schema: { params: TID_PARAMS } },
      async (req, reply) => {
        revokeDevice((await mustTenant(req.params.tid)).built.ctx, req.params.device_id);
        return reply.code(204).send();
      },
    );

    scope.post<{ Body: { api_key: string } }>(
      "/admin/v1/resolve-key",
      {
        schema: {
          body: {
            type: "object",
            additionalProperties: false,
            required: ["api_key"],
            properties: { api_key: { type: "string", minLength: 1, maxLength: 128 } },
          },
        },
      },
      async (req) => {
        const key = req.body.api_key;
        // An agent token (`at_…`) resolves like the tenant's API key while it is valid.
        const r = key.startsWith("at_") ? hub.registry.get(hub.agentTokenTenant(key) ?? "") : hub.registry.byApiKey(key);
        if (!r) throw notFound("API key");
        if (r.disabled) throw new ApiError(403, "tenant_disabled", "This account is disabled");
        return { tenant_id: r.tenant_id, base_url: hub.baseUrl(r.tenant_id) };
      },
    );
  });

  // ---------- shared LiveKit webhook, handed to the tenant named in the room ----------
  app.register(async (scope) => {
    const raw = (_req: unknown, body: string, done: (err: null, body: string) => void) => done(null, body);
    scope.addContentTypeParser("application/webhook+json", { parseAs: "string" }, raw);
    scope.addContentTypeParser("application/json", { parseAs: "string" }, raw);
    scope.post<{ Body: string }>("/v1/livekit/webhook", async (req, reply) => {
      if (req.headers["x-forwarded-for"] !== undefined) throw notFound("Route");
      let room: string | undefined;
      try {
        room = (JSON.parse(req.body) as { room?: { name?: string } }).room?.name;
      } catch {
        throw new ApiError(400, "invalid_request", "Bad webhook body");
      }
      const tid = room?.split(".", 1)[0];
      const t = tid && TENANT_ID.test(tid) ? await hub.tenant(tid) : undefined;
      // Not ours (or the tenant is gone): acknowledge so LiveKit doesn't retry.
      if (!t) return reply.code(200).send({ ok: true });
      // The tenant verifies the signature itself.
      const res = await t.built.app.inject({
        method: "POST",
        url: "/v1/livekit/webhook",
        headers: {
          "content-type": String(req.headers["content-type"] ?? "application/webhook+json"),
          ...(req.headers.authorization ? { authorization: req.headers.authorization } : {}),
        },
        payload: req.body,
      });
      return reply.code(res.statusCode).header("content-type", "application/json").send(res.body);
    });
  });

  app.addHook("onClose", async () => {
    await hub.close();
  });
  await hub.resumeOpenCalls();
  hub.startSweeper();
  return { app, hub };
}
