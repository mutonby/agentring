import { readFileSync } from "node:fs";
import http2 from "node:http2";
import { SignJWT, importPKCS8 } from "jose";
import type { PushResult } from "./push-provider.js";

export type ApnsEnv = "sandbox" | "production";
/** `voip` = PushKit (call.invite only); `alert` shows a notification; `background` wakes the app silently. */
export type ApnsPushType = "voip" | "alert" | "background";

export interface ApnsConfig {
  /** The `.p8` signing key from the Apple developer account (Keys → APNs). */
  keyFile: string;
  keyId: string;
  teamId: string;
  /** The app's bundle id; VoIP pushes go to `${topic}.voip`. */
  topic: string;
  /** Overridable for tests (cleartext h2c is fine there). */
  hosts: Record<ApnsEnv, string>;
}

export const APNS_HOSTS: Record<ApnsEnv, string> = {
  production: "https://api.push.apple.com",
  sandbox: "https://api.sandbox.push.apple.com",
};

export interface ApnsRequest {
  token: string;
  env: ApnsEnv;
  pushType: ApnsPushType;
  /** 10 = deliver now; 5 = when convenient (required for `background`). */
  priority: 5 | 10;
  /** Unix seconds after which APNs drops the push; 0 = try once, now. */
  expiration: number;
  collapseId?: string;
  payload: Record<string, unknown>;
}

/** What the dispatcher needs from APNs; a fake in tests. */
export interface ApnsSender {
  readonly topic: string;
  send(req: ApnsRequest): Promise<PushResult>;
  prepare?(): Promise<void>;
  close?(): void;
}

/** Apple accepts a provider token for up to an hour and rejects refreshing it more than every 20 minutes. */
const TOKEN_REFRESH_MS = 50 * 60_000;
const REQUEST_TIMEOUT_MS = 15_000;

/**
 * APNs over HTTP/2 with a token-based (JWT, ES256) connection: no certificates,
 * no extra dependency. One session per gateway, reused for every push.
 */
export class ApnsProvider implements ApnsSender {
  readonly name = "apns";
  private readonly keyPem: string;
  private jwt?: { value: string; issuedAt: number };
  private readonly sessions = new Map<ApnsEnv, http2.ClientHttp2Session>();

  constructor(private readonly cfg: ApnsConfig) {
    this.keyPem = readFileSync(cfg.keyFile, "utf8");
    if (!/^[A-Z0-9]{10}$/.test(cfg.keyId)) throw new Error("APNS_KEY_ID must be the 10-character key id");
    if (!/^[A-Z0-9]{10}$/.test(cfg.teamId)) throw new Error("APNS_TEAM_ID must be the 10-character team id");
  }

  get topic(): string {
    return this.cfg.topic;
  }

  async prepare(): Promise<void> {
    await this.token();
  }

  private async token(): Promise<string> {
    if (this.jwt && Date.now() - this.jwt.issuedAt < TOKEN_REFRESH_MS) return this.jwt.value;
    const key = await importPKCS8(this.keyPem, "ES256");
    const now = Math.floor(Date.now() / 1000);
    const value = await new SignJWT({})
      .setProtectedHeader({ alg: "ES256", kid: this.cfg.keyId })
      .setIssuer(this.cfg.teamId)
      .setIssuedAt(now)
      .sign(key);
    this.jwt = { value, issuedAt: Date.now() };
    return value;
  }

  private session(env: ApnsEnv): http2.ClientHttp2Session {
    const open = this.sessions.get(env);
    if (open && !open.closed && !open.destroyed) return open;
    const s = http2.connect(this.cfg.hosts[env]);
    // A dropped connection is reopened on the next push; never crash the relay over it.
    s.on("error", () => this.sessions.delete(env));
    s.on("close", () => this.sessions.delete(env));
    s.on("goaway", () => this.sessions.delete(env));
    s.unref();
    this.sessions.set(env, s);
    return s;
  }

  async send(req: ApnsRequest): Promise<PushResult> {
    let jwt: string;
    try {
      jwt = await this.token();
    } catch (e) {
      return { ok: false, unregistered: false, error: String(e) };
    }
    const headers: http2.OutgoingHttpHeaders = {
      ":method": "POST",
      ":path": `/3/device/${req.token}`,
      authorization: `bearer ${jwt}`,
      "apns-topic": req.pushType === "voip" ? `${this.cfg.topic}.voip` : this.cfg.topic,
      "apns-push-type": req.pushType,
      "apns-priority": String(req.priority),
      "apns-expiration": String(req.expiration),
      ...(req.collapseId ? { "apns-collapse-id": req.collapseId } : {}),
      "content-type": "application/json",
    };
    try {
      const { status, body } = await this.request(req.env, headers, JSON.stringify(req.payload));
      if (status === 200) return { ok: true };
      const reason = safeReason(body);
      // 410 = the token is no longer active; BadDeviceToken = wrong token or wrong gateway.
      const unregistered = status === 410 || reason === "Unregistered" || reason === "BadDeviceToken";
      if (status === 403 && reason === "ExpiredProviderToken") this.jwt = undefined;
      return { ok: false, unregistered, error: `${status} ${reason ?? body.slice(0, 200)}` };
    } catch (e) {
      return { ok: false, unregistered: false, error: String(e) };
    }
  }

  private request(env: ApnsEnv, headers: http2.OutgoingHttpHeaders, body: string): Promise<{ status: number; body: string }> {
    return new Promise((resolve, reject) => {
      const s = this.session(env);
      const stream = s.request(headers);
      let status = 0;
      let data = "";
      stream.setEncoding("utf8");
      stream.setTimeout(REQUEST_TIMEOUT_MS, () => stream.close(http2.constants.NGHTTP2_CANCEL));
      stream.on("response", (h) => (status = Number(h[":status"])));
      stream.on("data", (chunk: string) => (data += chunk));
      stream.on("end", () => resolve({ status, body: data }));
      stream.on("close", () => status === 0 && reject(new Error("APNs stream closed without a response")));
      stream.on("error", reject);
      stream.end(body);
    });
  }

  close(): void {
    for (const s of this.sessions.values()) s.close();
    this.sessions.clear();
  }
}

function safeReason(body: string): string | undefined {
  try {
    return (JSON.parse(body) as { reason?: string }).reason;
  } catch {
    return undefined;
  }
}
