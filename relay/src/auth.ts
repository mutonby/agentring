import { createHash, timingSafeEqual } from "node:crypto";
import { SignJWT, jwtVerify } from "jose";
import type { Db } from "./db/database.js";
import { unauthorized } from "./errors.js";

export type Principal = { kind: "agent" } | { kind: "device"; deviceId: string };

export function sha256(s: string): Buffer {
  return createHash("sha256").update(s).digest();
}

export class Auth {
  private readonly jwtKey: Uint8Array;

  /** `apiKeyHash` returns the SHA-256 of the current agent API key (it may change: hub mode rotates keys). */
  constructor(
    private readonly db: Db,
    private readonly apiKeyHash: () => Buffer,
    jwtSecret: string,
    private readonly relayId: string,
    /** Hub mode: whether a short-lived agent token (`at_…`, by its SHA-256) is valid for this tenant. */
    private readonly agentToken?: (hash: Buffer) => boolean,
  ) {
    this.jwtKey = Buffer.from(jwtSecret, "base64url");
  }

  async issueDeviceJwt(deviceId: string): Promise<string> {
    return new SignJWT({})
      .setProtectedHeader({ alg: "HS256", typ: "JWT" })
      .setIssuer(this.relayId)
      .setSubject(deviceId)
      .setAudience("device")
      .setIssuedAt()
      .sign(this.jwtKey);
  }

  /** Resolves an `Authorization` header value to a principal, or throws 401. */
  async authenticate(header: string | undefined): Promise<Principal> {
    const m = header?.match(/^Bearer\s+(\S+)$/i);
    if (!m) throw unauthorized();
    const token = m[1]!;

    if (token.startsWith("ak_")) {
      if (timingSafeEqual(sha256(token), this.apiKeyHash())) return { kind: "agent" };
      throw unauthorized();
    }

    if (token.startsWith("at_")) {
      if (this.agentToken?.(sha256(token))) return { kind: "agent" };
      throw unauthorized();
    }

    let deviceId: string;
    try {
      const { payload } = await jwtVerify(token, this.jwtKey, {
        issuer: this.relayId,
        audience: "device",
        algorithms: ["HS256"],
      });
      if (typeof payload.sub !== "string") throw new Error("no sub");
      deviceId = payload.sub;
    } catch {
      throw unauthorized();
    }

    const row = this.db.prepare("SELECT revoked_at FROM devices WHERE device_id = ?").get(deviceId) as
      | { revoked_at: number | null }
      | undefined;
    if (!row || row.revoked_at !== null) throw unauthorized("Device revoked or unknown");
    this.db.prepare("UPDATE devices SET last_seen_at = ? WHERE device_id = ?").run(Date.now(), deviceId);
    return { kind: "device", deviceId };
  }
}

export function partyOf(p: Principal): "agent" | "user" {
  return p.kind === "agent" ? "agent" : "user";
}
