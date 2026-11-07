import { randomBytes } from "node:crypto";
import { setTimeout as sleep } from "node:timers/promises";
import type { FastifyInstance } from "fastify";
import type { WebSocket } from "ws";
import type { Principal } from "../auth.js";
import { type Ctx, principal, requireAuth } from "../context.js";
import type { EventLog } from "../events.js";
import { newId } from "../ids.js";
import type { Event } from "../protocol.js";

const PING_INTERVAL_MS = 20_000;
const DEAD_AFTER_MS = 60_000;
const REPLAY_PAGE = 200;
/** A client this far behind is dropped; it catches up with `after_seq` when it reconnects. */
const MAX_BUFFERED_BYTES = 4 * 1024 * 1024;
/** Replay waits for the socket to drain to this level before sending the next page. */
const REPLAY_LOW_WATER_BYTES = 256 * 1024;
const REPLAY_DRAIN_POLL_MS = 20;
/** A replaying client that reads nothing for this long is dropped. */
const REPLAY_STALL_MS = 30_000;
/** Live events kept for a socket while it replays. */
const MAX_PENDING_EVENTS = 1_000;
/** Sockets per phone, oldest dropped first: a reconnecting phone must not lock itself out. */
const MAX_DEVICE_SOCKETS = 4;
/** Sockets per relay or tenant, over which a new one is refused. */
const MAX_SOCKETS = 64;

export const CLOSE_UNAUTHORIZED = 4401;
export const CLOSE_REPLACED = 4409;
const CLOSE_TRY_LATER = 1013;

/** Tracks open sockets: one daemon (newest wins) and any number of device sockets. */
export class WsHub {
  private agent?: WebSocket;
  private readonly devices = new Map<string, Set<WebSocket>>();
  /** Every open socket, also those still replaying. */
  private readonly sockets = new Set<WebSocket>();

  constructor(private readonly events: EventLog) {}

  /** Counts a new socket, or returns false when the relay is at its limit. */
  track(ws: WebSocket): boolean {
    if (this.sockets.size >= MAX_SOCKETS) return false;
    this.sockets.add(ws);
    return true;
  }

  untrack(ws: WebSocket): void {
    this.sockets.delete(ws);
  }

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
    for (const old of set) {
      if (set.size <= MAX_DEVICE_SOCKETS) break;
      set.delete(old);
      old.close(CLOSE_TRY_LATER, "too many sockets for this device");
    }
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
    return this.sockets.size;
  }

  disconnectDevice(deviceId: string): void {
    for (const ws of this.devices.get(deviceId) ?? []) ws.close(CLOSE_UNAUTHORIZED, "device revoked");
  }

  closeAll(): void {
    for (const ws of this.sockets) ws.close(1001, "server shutting down");
  }
}

/** Waits for the socket to take the data it was given; drops a client that stops reading. */
async function drained(socket: WebSocket): Promise<void> {
  const deadline = Date.now() + REPLAY_STALL_MS;
  while (socket.readyState === socket.OPEN && socket.bufferedAmount > REPLAY_LOW_WATER_BYTES) {
    if (Date.now() > deadline) {
      socket.terminate();
      return;
    }
    await sleep(REPLAY_DRAIN_POLL_MS);
  }
  await new Promise<void>((resolve) => setImmediate(resolve)); // let other work run between pages
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
      if (!hub.track(socket)) {
        socket.close(CLOSE_TRY_LATER, "too many sockets");
        return;
      }
      const open = () => socket.readyState === socket.OPEN;
      const send = (e: Event) => {
        if (!open()) return;
        if (socket.bufferedAmount > MAX_BUFFERED_BYTES) {
          socket.terminate();
          return;
        }
        socket.send(JSON.stringify(e));
      };

      // Replay is paced by the client, so live events can arrive during it: subscribe
      // first and hold them, then send those the replay didn't already cover.
      let replaying = true;
      const pending: Event[] = [];
      const unsubscribe = events.subscribe({
        principal: p,
        send: (e) => {
          if (!replaying) send(e);
          else if (pending.length < MAX_PENDING_EVENTS) pending.push(e);
          else socket.terminate();
        },
      });
      if (p.kind === "device") hub.attachDevice(p.deviceId, socket);

      // Only a pong echoing the current nonce proves the client reads what it is sent.
      let nonce = randomBytes(8);
      let lastPong = Date.now();
      socket.on("pong", (data: Buffer) => {
        if (data.equals(nonce)) lastPong = Date.now();
      });
      const timer = setInterval(() => {
        if (Date.now() - lastPong > DEAD_AFTER_MS) {
          socket.terminate();
          return;
        }
        nonce = randomBytes(8);
        socket.ping(nonce);
      }, PING_INTERVAL_MS);

      // A short-lived agent token stops working when it expires, on this socket too.
      const expiry =
        p.kind === "agent" && p.expiresAt !== undefined
          ? setTimeout(() => socket.close(CLOSE_UNAUTHORIZED, "agent token expired"), Math.max(0, p.expiresAt - Date.now()))
          : undefined;
      expiry?.unref();

      socket.on("close", () => {
        clearInterval(timer);
        clearTimeout(expiry);
        unsubscribe();
        hub.untrack(socket);
        if (p.kind === "agent") hub.detachAgent(socket);
        else hub.detachDevice(p.deviceId, socket);
      });
      // Clients don't send frames in v1; ignore anything they do send.
      socket.on("message", () => undefined);

      const replay = async () => {
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
          after = page.nextAfterSeq;
          if (!page.hasMore) break;
          await drained(socket);
          if (!open()) return;
        }
        replaying = false;
        for (const e of pending) if (e.seq === undefined || e.seq > after) send(e);
        pending.length = 0;

        if (p.kind === "agent") hub.attachAgent(socket);
        else send(ephemeral({ type: "agent.presence", online: hub.isAgentOnline() } as Omit<Event, "id" | "ts">));
      };
      replay().catch((err: unknown) => {
        req.log.error({ err }, "websocket replay failed");
        socket.close(1011, "internal error");
      });
    },
  );
}
