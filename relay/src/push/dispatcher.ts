import { createHmac } from "node:crypto";
import type { FastifyBaseLogger } from "fastify";
import type { Db } from "../db/database.js";
import type { Event } from "../protocol.js";
import type { ApnsEnv, ApnsRequest, ApnsSender } from "./apns.js";
import type { PushPayload, PushProvider, PushResult } from "./push-provider.js";

/** FCM data payloads are capped at 4096 bytes; leave room for the other keys. */
const MAX_EVENT_BYTES = 3000;
const MESSAGE_TTL_S = 28 * 24 * 3600;
/** Call state changes only matter while a phone may still be ringing or on the call. */
const CALL_UPDATE_TTL_S = 30;
/** "Someone is on the call, you can join": long enough for a phone that was offline; the app re-checks the call. */
const CALL_JOINABLE_TTL_S = 3600;
/** iOS shows missed calls from the push itself, so it may arrive late. */
const MISSED_CALL_TTL_S = 24 * 3600;

/** Just enough of a call for push routing; `device_ids` = the phones a targeted call rings. */
export type CallLookup = (callId: string) => { direction: string; device_ids?: string[] } | undefined;

export function signEvent(pushSecret: string, eventJson: string): string {
  return createHmac("sha256", Buffer.from(pushSecret, "base64url")).update(eventJson, "utf8").digest("base64url");
}

export function buildPayload(relayId: string, pushSecret: string, event: Event, maxEventBytes = MAX_EVENT_BYTES): PushPayload {
  let json = JSON.stringify(event);
  if (event.type === "msg.new") {
    let text = event.text;
    while (Buffer.byteLength(json) > maxEventBytes && text.length > 0) {
      const overflow = Buffer.byteLength(json) - maxEventBytes;
      // Cut by code points so we never split a surrogate pair.
      const chars = Array.from(text);
      text = chars.slice(0, Math.max(0, chars.length - Math.max(overflow, 16))).join("");
      json = JSON.stringify({ ...event, text, truncated: true });
    }
  }
  return { v: "1", relay_id: relayId, event: json, sig: signEvent(pushSecret, json) };
}

export interface PushRule {
  ttlSeconds: number;
  highPriority: boolean;
}

/** Which events wake the phone. Everything else is delivered over the WebSocket only. */
function ruleFor(event: Event, getCall: CallLookup): PushRule | undefined {
  switch (event.type) {
    case "msg.new":
      return event.from === "agent" ? { ttlSeconds: MESSAGE_TTL_S, highPriority: true } : undefined;
    case "call.invite":
      // Never deliver an invite after it expired: TTL = time left to ring.
      return event.direction === "agent_to_user"
        ? { ttlSeconds: Math.max(1, Math.ceil((event.expires_at - Date.now()) / 1000)), highPriority: true }
        : undefined;
    case "call.accept":
      // Lets the other phones offer "join the call" (also those whose ring ended or
      // that never got the invite). Normal priority: it doesn't need to wake a phone
      // in Doze, and a high-priority push that shows nothing (the phones still
      // ringing) gets the app's later pushes deprioritized.
      return event.by === "user" && getCall(event.call_id)?.direction === "agent_to_user"
        ? { ttlSeconds: CALL_JOINABLE_TTL_S, highPriority: false }
        : undefined;
    // call.decline is not pushed: in a group call one phone declining doesn't affect the others.
    case "call.end":
    case "call.missed":
      // Stops the ringing on every device (or tears down the call screen).
      return getCall(event.call_id)?.direction === "agent_to_user"
        ? { ttlSeconds: CALL_UPDATE_TTL_S, highPriority: true }
        : undefined;
    default:
      return undefined;
  }
}

/**
 * The APNs request for an event on an iPhone, or undefined if the event isn't
 * pushed there (or the needed token is missing). iOS rules (docs/ios.md):
 * - `call.invite` is the only VoIP push: iOS kills apps that get a VoIP push
 *   without reporting a CallKit call, so nothing else may use that token.
 * - `msg.new` and `call.missed` are alerts with `mutable-content`: the app's
 *   notification extension verifies `sig` and fills in the text. Texts are
 *   `loc-key`s, so a push whose extension fails still reads well (and leaks nothing).
 * - `call.accept` ("join the call") is a passive alert, replaced per call.
 * - `call.end` is silent: it lets the app clear the call's notifications.
 */
export function apnsRequestFor(
  event: Event,
  rule: PushRule,
  data: PushPayload,
  tokens: { apns: string | null; voip: string | null; env: ApnsEnv },
  agentName: string,
  now = Date.now(),
): ApnsRequest | undefined {
  const expiration = Math.ceil(now / 1000) + rule.ttlSeconds;
  const regular = (pushType: ApnsRequest["pushType"], priority: 5 | 10, aps: Record<string, unknown>, collapseId?: string) =>
    tokens.apns
      ? { token: tokens.apns, env: tokens.env, pushType, priority, expiration, ...(collapseId ? { collapseId } : {}), payload: { aps, ...data } }
      : undefined;
  switch (event.type) {
    case "call.invite":
      return tokens.voip
        ? { token: tokens.voip, env: tokens.env, pushType: "voip", priority: 10, expiration: Math.ceil(event.expires_at / 1000), payload: { aps: {}, ...data } }
        : undefined;
    case "msg.new":
      return regular("alert", 10, {
        alert: { title: agentName, "loc-key": "PUSH_MSG_NEW" },
        sound: "default",
        "mutable-content": 1,
        "thread-id": "messages",
        category: "MSG_NEW",
      });
    case "call.missed": {
      // The ringing stops over the socket; this is the "missed call" the user reads later.
      const req = regular(
        "alert",
        10,
        { alert: { title: agentName, "loc-key": "PUSH_CALL_MISSED" }, sound: "default", "mutable-content": 1, "thread-id": "calls" },
        event.call_id,
      );
      return req && { ...req, expiration: Math.ceil(now / 1000) + MISSED_CALL_TTL_S };
    }
    case "call.accept":
      return regular(
        "alert",
        10,
        {
          alert: { title: agentName, "loc-key": "PUSH_CALL_JOINABLE" },
          "interruption-level": "passive",
          "mutable-content": 1,
          "thread-id": "calls",
          category: "CALL_JOINABLE",
        },
        event.call_id,
      );
    case "call.end":
      return regular("background", 5, { "content-available": 1 });
    default:
      return undefined;
  }
}

/** iOS events carry the alert and the signed event in one payload; APNs caps it at 4096 bytes (5120 for VoIP). */
const APNS_MAX_EVENT_BYTES = 3000;

interface DeviceRow {
  device_id: string;
  platform: string;
  push_secret: string;
  fcm_token: string | null;
  apns_token: string | null;
  voip_token: string | null;
  apns_env: string | null;
}

export class PushDispatcher {
  constructor(
    private readonly db: Db,
    private readonly provider: PushProvider | undefined,
    private readonly relayId: string,
    private readonly log: FastifyBaseLogger,
    private readonly getCall: CallLookup = () => undefined,
    private readonly apns?: ApnsSender,
    private readonly agentName = "Agent",
  ) {}

  handle(event: Event): void {
    if (!this.provider && !this.apns) return;
    const rule = ruleFor(event, this.getCall);
    if (!rule) return;
    const devices = this.db
      .prepare(
        `SELECT device_id, platform, push_secret, fcm_token, apns_token, voip_token, apns_env FROM devices
         WHERE revoked_at IS NULL AND (fcm_token IS NOT NULL OR apns_token IS NOT NULL OR voip_token IS NOT NULL)`,
      )
      .all() as unknown as DeviceRow[];
    // A targeted call's pushes only go to the phones it targets.
    const targets = event.type.startsWith("call.") ? this.getCall((event as { call_id: string }).call_id)?.device_ids : undefined;
    for (const d of devices) {
      if (targets && !targets.includes(d.device_id)) continue;
      // The phone that joined already knows.
      if (event.type === "call.accept" && event.device_id === d.device_id) continue;
      if (d.platform === "ios") this.sendApns(event, rule, d);
      else this.sendFcm(event, rule, d);
    }
  }

  private sendFcm(event: Event, rule: PushRule, d: DeviceRow): void {
    if (!this.provider || !d.fcm_token) return;
    const token = d.fcm_token;
    const payload = buildPayload(this.relayId, d.push_secret, event);
    this.track(event, d, this.provider.send({ deviceId: d.device_id, token }, payload, rule), "fcm_token", token);
  }

  private sendApns(event: Event, rule: PushRule, d: DeviceRow): void {
    if (!this.apns) return;
    const data = buildPayload(this.relayId, d.push_secret, event, APNS_MAX_EVENT_BYTES);
    const env: ApnsEnv = d.apns_env === "sandbox" ? "sandbox" : "production";
    const req = apnsRequestFor(event, rule, data, { apns: d.apns_token, voip: d.voip_token, env }, this.agentName);
    if (!req) return;
    const column = req.pushType === "voip" ? "voip_token" : "apns_token";
    this.track(event, d, this.apns.send(req), column, req.token);
  }

  private track(event: Event, d: DeviceRow, sent: Promise<PushResult>, column: "fcm_token" | "apns_token" | "voip_token", token: string): void {
    void sent
      .then((res) => {
        if (res.ok) {
          this.log.debug({ device: d.device_id, event: event.type }, "push sent");
          return;
        }
        this.log.warn({ device: d.device_id, event: event.type, error: res.error }, "push failed");
        if (res.unregistered) {
          // Only forget the token that failed (the phone may have sent a new one meanwhile).
          this.db.prepare(`UPDATE devices SET ${column} = NULL WHERE device_id = ? AND ${column} = ?`).run(d.device_id, token);
        }
      })
      .catch((e: unknown) => this.log.error({ err: e, device: d.device_id }, "push error"));
  }
}
