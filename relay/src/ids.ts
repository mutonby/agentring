import { randomBytes } from "node:crypto";

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const RANDOM_MAX = (1n << 80n) - 1n;

/** Replaces lone surrogates, which are not valid text and break strict JSON and UTF-8 consumers. */
export const wellFormed = (s: string): string => s.replace(/\p{Cs}/gu, "\uFFFD");

let lastTime = -1;
let lastRandom = 0n;

function encode(value: bigint, length: number): string {
  let out = "";
  for (let i = 0; i < length; i++) {
    out = CROCKFORD[Number(value % 32n)] + out;
    value /= 32n;
  }
  return out;
}

/**
 * Monotonic ULID: 48-bit ms timestamp + 80-bit random, Crockford base32
 * (26 chars). IDs created in the same millisecond still sort in creation
 * order, so they can be used as pagination cursors.
 */
export function ulid(now = Date.now()): string {
  if (now <= lastTime) {
    now = lastTime;
    lastRandom = lastRandom === RANDOM_MAX ? 0n : lastRandom + 1n;
    if (lastRandom === 0n) now = ++lastTime;
  } else {
    lastRandom = BigInt("0x" + randomBytes(10).toString("hex")) >> 1n; // leave headroom for increments
  }
  lastTime = now;
  return encode(BigInt(now), 10) + encode(lastRandom, 16);
}

export type IdPrefix = "rly" | "dev" | "call" | "msg" | "evt" | "tnt";

export function newId(prefix: IdPrefix): string {
  return `${prefix}_${ulid()}`;
}

export function randomToken(prefix: string, bytes = 32): string {
  return `${prefix}_${randomBytes(bytes).toString("base64url")}`;
}
