import { randomBytes } from "node:crypto";
import { chmodSync, existsSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import websocket from "@fastify/websocket";
import Fastify, { type FastifyInstance, type FastifyServerOptions } from "fastify";
import { Auth } from "./auth.js";
import { type Config, dbPath } from "./config.js";
import { type Ctx, WS_SUBPROTOCOL } from "./context.js";
import { type Db, metaOrInit, openDatabase } from "./db/database.js";
import { ApiError } from "./errors.js";
import { EventLog } from "./events.js";
import { newId, randomToken } from "./ids.js";
import { LiveKitServer, type LiveKitService } from "./livekit.js";
import type { PushConfig } from "./protocol.js";
import { PushDispatcher } from "./push/dispatcher.js";
import { FcmProvider } from "./push/fcm.js";
import type { PushProvider } from "./push/push-provider.js";
import { Calls, callRoutes, targetsOf } from "./routes/calls.js";
import { messageRoutes } from "./routes/messages.js";
import { pairingRoutes } from "./routes/pairing.js";
import { systemRoutes } from "./routes/system.js";
import { webRoutes } from "./routes/web.js";
import { WsHub, wsRoutes } from "./routes/ws.js";

export interface BuildOptions {
  logger?: FastifyServerOptions["logger"];
  /** Inject a push provider (tests). Defaults to FCM when configured. */
  pushProvider?: PushProvider;
  /** Inject LiveKit (tests). Defaults to the configured server, if any. */
  livekit?: LiveKitService;
}

export interface BuiltApp {
  app: FastifyInstance;
  ctx: Ctx;
  apiKey: string;
  /** True when the API key was generated on this start (print it once). */
  apiKeyGenerated: boolean;
}

/**
 * The API key comes from AGENTRING_API_KEY or is generated on first start and
 * persisted in the database's meta table and in DATA_DIR/api_key (mode 0600), so
 * the operator can read it once. Requests are checked against its hash.
 */
function resolveApiKey(cfg: Config, db: Db): { apiKey: string; generated: boolean } {
  if (cfg.apiKey) {
    if (!cfg.apiKey.startsWith("ak_")) throw new Error("AGENTRING_API_KEY must start with ak_");
    return { apiKey: cfg.apiKey, generated: false };
  }
  let generated = false;
  const apiKey = metaOrInit(db, "api_key", () => {
    generated = true;
    return randomToken("ak");
  });
  if (cfg.dataDir !== ":memory:") {
    const file = join(cfg.dataDir, "api_key");
    if (!existsSync(file)) {
      writeFileSync(file, apiKey + "\n", { mode: 0o600 });
      chmodSync(file, 0o600);
    }
  }
  return { apiKey, generated };
}

export async function buildApp(cfg: Config, opts: BuildOptions = {}): Promise<BuiltApp> {
  const db = openDatabase(dbPath(cfg));
  const relayId = metaOrInit(db, "relay_id", () => newId("rly"));
  const jwtSecret = metaOrInit(db, "jwt_secret", () => randomBytes(32).toString("base64url"));
  const { apiKey, generated } = resolveApiKey(cfg, db);

  const app = Fastify({
    logger: opts.logger ?? true,
    trustProxy: true,
    bodyLimit: 64 * 1024,
  });
  await app.register(websocket, {
    options: {
      maxPayload: 64 * 1024,
      // Browsers send their token as a subprotocol (see requireAuth); never echo that one back.
      handleProtocols: (offered) => (offered.has(WS_SUBPROTOCOL) ? WS_SUBPROTOCOL : false),
    },
  });

  const provider: PushProvider | undefined =
    opts.pushProvider ?? (cfg.fcm ? new FcmProvider(cfg.fcm.serviceAccountFile, cfg.fcm.apiBase) : undefined);
  const pushConfig: PushConfig = cfg.fcm ? { provider: "fcm", fcm: cfg.fcm.client } : null;
  if (!provider) app.log.warn("Push is not configured: the phone will only get events while the app is open");

  const events = new EventLog(db);
  const ctx: Ctx = {
    cfg,
    db,
    auth: new Auth(db, apiKey, jwtSecret, relayId),
    events,
    relayId,
    pushConfig,
    pushProviderName: provider?.name ?? "none",
    hub: new WsHub(events),
    livekit: opts.livekit ?? (cfg.livekit ? new LiveKitServer(cfg.livekit) : undefined),
  };
  if (!ctx.livekit) app.log.warn("LiveKit is not configured: calls are disabled");
  const calls = new Calls(ctx);
  ctx.calls = calls;
  events.setVisibility((e, p) => calls.eventVisibleTo(e, p));

  const dispatcher = new PushDispatcher(db, provider, relayId, app.log, (id) => {
    const c = calls.get(id);
    return c && { direction: c.direction, device_ids: targetsOf(c) };
  });
  events.onEvent((e) => dispatcher.handle(e));

  app.setErrorHandler((err, req, reply) => {
    if (err instanceof ApiError) {
      return reply.code(err.statusCode).send({ error: { code: err.code, message: err.message } });
    }
    const e = err as { validation?: unknown; statusCode?: number; message: string };
    if (e.validation || e.statusCode === 400) {
      return reply.code(400).send({ error: { code: "invalid_request", message: e.message } });
    }
    if (e.statusCode && e.statusCode < 500) {
      return reply.code(e.statusCode).send({ error: { code: "invalid_request", message: e.message } });
    }
    req.log.error({ err }, "unhandled error");
    return reply.code(500).send({ error: { code: "internal", message: "Internal error" } });
  });
  app.setNotFoundHandler((req, reply) =>
    reply.code(404).send({ error: { code: "not_found", message: `${req.method} ${req.url} not found` } }),
  );

  systemRoutes(app, ctx);
  pairingRoutes(app, ctx);
  messageRoutes(app, ctx);
  callRoutes(app, ctx, calls);
  calls.resume();
  // Fetch the FCM access token now so the first call's push doesn't wait for it.
  void provider?.prepare?.().catch((err: unknown) => app.log.warn({ err }, "push provider warm-up failed"));
  wsRoutes(app, ctx);
  webRoutes(app, ctx);

  app.addHook("onClose", async () => {
    calls.stop();
    ctx.hub.closeAll();
    db.close();
  });

  return { app, ctx, apiKey, apiKeyGenerated: generated };
}
