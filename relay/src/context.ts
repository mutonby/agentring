import type { FastifyRequest } from "fastify";
import type { Auth, Principal } from "./auth.js";
import type { Config } from "./config.js";
import type { Db } from "./db/database.js";
import type { EventLog } from "./events.js";
import type { LiveKitService } from "./livekit.js";
import { forbidden } from "./errors.js";
import type { PushConfig } from "./protocol.js";
import type { Calls } from "./routes/calls.js";
import type { WsHub } from "./routes/ws.js";

export interface Ctx {
  cfg: Config;
  db: Db;
  auth: Auth;
  events: EventLog;
  relayId: string;
  pushConfig: PushConfig;
  pushProviderName: string;
  hub: WsHub;
  /** Undefined when LiveKit isn't configured: calls are then unavailable. */
  livekit?: LiveKitService;
  calls?: Calls;
}

declare module "fastify" {
  interface FastifyRequest {
    principal?: Principal;
  }
}

type Kind = Principal["kind"];

/** The subprotocol a browser WebSocket offers next to `bearer.<token>`; the relay selects it. */
export const WS_SUBPROTOCOL = "agentring.v1";
const BEARER_SUBPROTOCOL = "bearer.";

/**
 * The request's credentials as an `Authorization` value. Browsers can't set
 * headers on a WebSocket, so an upgrade may carry the token as the
 * `bearer.<token>` subprotocol instead; the header wins when both are present.
 */
function authorizationOf(req: FastifyRequest): string | undefined {
  if (req.headers.authorization !== undefined || req.headers.upgrade?.toLowerCase() !== "websocket") {
    return req.headers.authorization;
  }
  const offered = req.headers["sec-websocket-protocol"]?.split(",").map((s) => s.trim()) ?? [];
  const token = offered.find((s) => s.startsWith(BEARER_SUBPROTOCOL))?.slice(BEARER_SUBPROTOCOL.length);
  return token ? `Bearer ${token}` : undefined;
}

/** Fastify preHandler that authenticates and restricts to the given principal kinds. */
export function requireAuth(ctx: Ctx, ...kinds: Kind[]) {
  return async (req: FastifyRequest) => {
    const p = await ctx.auth.authenticate(authorizationOf(req));
    if (!kinds.includes(p.kind)) throw forbidden(`This endpoint is not available to ${p.kind} credentials`);
    req.principal = p;
  };
}

export function principal(req: FastifyRequest): Principal {
  if (!req.principal) throw new Error("route is missing requireAuth");
  return req.principal;
}
