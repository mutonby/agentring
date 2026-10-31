import type { FastifyInstance } from "fastify";
import type { WebSocket } from "ws";
import type { Principal } from "../auth.js";
import { type Ctx, principal, requireAuth } from "../context.js";
import type { EventLog } from "../events.js";
import { newId } from "../ids.js";
import type { Event } from "../protocol.js";

const PING_INTERVAL_MS = 20_000;
const DEAD_AFTER_MS = 60_000;
const REPLAY_PAGE = 500;

export const CLOSE_UNAUTHORIZED = 4401;
export const CLOSE_REPLACED = 4409;

/** Tracks open sockets: one daemon (newest wins) and any number of device sockets. */
export class WsHub {
  private agent?: WebSocket;
  private readonly devices = new Map<string, Set<WebSocket>>();

  constructor(private readonly events: EventLog) {}

  isAgentOnline(): boolean {
    return this.agent !== undefined;
  }

  attachAgent(ws: WebSocket): void {
    const previous = this.agent;
    this.agent = ws;
    if (previous) previous.close(CLOSE_REPLACED, "replaced by a newer agent connection");
    else this.events.emit({ type: "agent.presence", online: true }, "devices");
  }

  detachAgent(ws: WebSocket): void {
    if (this.agent !== ws) return;
    this.agent = undefined;
    this.events.emit({ type: "agent.presence", online: false }, "devices");
  }

  attachDevice(deviceId: string, ws: WebSocket): void {
    let set = this.devices.get(deviceId);
    if (!set) this.devices.set(deviceId, (set = new Set()));
    set.add(ws);
  }

  detachDevice(deviceId: string, ws: WebSocket): void {
    const set = this.devices.get(deviceId);
    set?.delete(ws);
    if (set?.size === 0) this.devices.delete(deviceId);
  }

  /** Closes the agent's socket, e.g. after its API key was rotated. */
  disconnectAgent(): void {
    this.agent?.close(CLOSE_UNAUTHORIZED, "API key rotated");
  }

  /** Open sockets (agent and devices); hub mode keeps a tenant loaded while it has any. */
  socketCount(): number {
    let n = this.agent ? 1 : 0;
    for (const set of this.devices.values()) n += set.size;
    return n;
  }

  disconnectDevice(deviceId: string): void {
    for (const ws of this.devices.get(deviceId) ?? []) ws.close(CLOSE_UNAUTHORIZED, "device revoked");
  }

  closeAll(): void {
    this.agent?.close(1001, "server shutting down");
    for (const set of this.devices.values()) for (const ws of set) ws.close(1001, "server shutting down");
  }
}

function ephemeral(event: Omit<Event, "id" | "ts">): Event {
  return { ...event, id: newId("evt"), ts: Date.now() } as Event;
}

export function wsRoutes(app: FastifyInstance, ctx: Ctx): void {
  const { events, hub } = ctx;

  app.get<{ Querystring: { after_seq?: number } }>(
    "/v1/ws",
    {
      websocket: true,
      preHandler: requireAuth(ctx, "agent", "device"),
      schema: { querystring: { type: "object", properties: { after_seq: { type: "integer", minimum: 0 } } } },
    },
    (socket, req) => {
      const p: Principal = principal(req);
      const send = (e: Event) => {
        if (socket.readyState === socket.OPEN) socket.send(JSON.stringify(e));
      };

      // Everything below runs synchronously, so no event can slip in between
      // the replay and the live subscription.
      send(
        ephemeral({ type: "ws.hello", relay_id: ctx.relayId, last_seq: events.lastSeq(), server_ts: Date.now() } as Omit<
          Event,
          "id" | "ts"
        >),
      );
      let after = req.query.after_seq ?? 0;
      for (;;) {
        const page = events.list(after, REPLAY_PAGE, p);
        page.events.forEach(send);
        if (!page.hasMore) break;
        after = page.nextAfterSeq;
      }
      const unsubscribe = events.subscribe({ principal: p, send });

      if (p.kind === "agent") {
        hub.attachAgent(socket);
      } else {
        hub.attachDevice(p.deviceId, socket);
        send(ephemeral({ type: "agent.presence", online: hub.isAgentOnline() } as Omit<Event, "id" | "ts">));
      }

      let lastPong = Date.now();
      socket.on("pong", () => (lastPong = Date.now()));
      const timer = setInterval(() => {
        if (Date.now() - lastPong > DEAD_AFTER_MS) {
          socket.terminate();
          return;
        }
        socket.ping();
      }, PING_INTERVAL_MS);

      socket.on("close", () => {
        clearInterval(timer);
        unsubscribe();
        if (p.kind === "agent") hub.detachAgent(socket);
        else hub.detachDevice(p.deviceId, socket);
      });
      // Clients don't send frames in v1; ignore anything they do send.
      socket.on("message", () => undefined);
    },
  );
}
