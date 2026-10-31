import type { Db } from "./db/database.js";
import { quotaExceeded } from "./errors.js";

/**
 * Per-tenant limits (hub mode, docs/cloud.md). `null` = unlimited. A self-hosted
 * relay has no limits.
 */
export interface Limits {
  max_devices: number | null;
  call_seconds_per_month: number | null;
  messages_per_day: number | null;
}

export const UNLIMITED: Limits = { max_devices: null, call_seconds_per_month: null, messages_per_day: null };

export interface Usage {
  /** UTC calendar month, e.g. "2026-11". */
  month: string;
  call_seconds: number;
  calls: number;
  messages_today: number;
  devices: number;
}

export function monthStart(now = Date.now()): number {
  const d = new Date(now);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1);
}

export function dayStart(now = Date.now()): number {
  const d = new Date(now);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
}

/** Answered call time this UTC month, in ms (an active call counts up to now). */
export function callMsThisMonth(db: Db, now = Date.now()): number {
  const r = db
    .prepare(
      `SELECT COALESCE(SUM(MAX(0, MIN(COALESCE(ended_at, ?), ?) - answered_at)), 0) AS ms
       FROM calls WHERE answered_at IS NOT NULL AND answered_at >= ?`,
    )
    .get(now, now, monthStart(now)) as { ms: number };
  return Number(r.ms);
}

function count(db: Db, sql: string, ...args: number[]): number {
  return Number((db.prepare(sql).get(...args) as { n: number }).n);
}

const activeDevices = (db: Db) => count(db, "SELECT COUNT(*) AS n FROM devices WHERE revoked_at IS NULL");
const messagesToday = (db: Db, now: number) =>
  count(db, "SELECT COUNT(*) AS n FROM messages WHERE created_at >= ?", dayStart(now));

export function usage(db: Db, now = Date.now()): Usage {
  const d = new Date(now);
  return {
    month: `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`,
    call_seconds: Math.floor(callMsThisMonth(db, now) / 1000),
    calls: count(db, "SELECT COUNT(*) AS n FROM calls WHERE created_at >= ?", monthStart(now)),
    messages_today: messagesToday(db, now),
    devices: activeDevices(db),
  };
}

export function checkPair(db: Db, limits: Limits): void {
  if (limits.max_devices !== null && activeDevices(db) >= limits.max_devices) {
    throw quotaExceeded(`Your plan allows ${limits.max_devices} paired phone(s); unpair one first`);
  }
}

export function checkMessage(db: Db, limits: Limits): void {
  if (limits.messages_per_day !== null && messagesToday(db, Date.now()) >= limits.messages_per_day) {
    throw quotaExceeded(`Your plan allows ${limits.messages_per_day} messages per day`);
  }
}

/** Call time left this month in ms, or undefined when unlimited. */
export function callMsLeft(db: Db, limits: Limits): number | undefined {
  if (limits.call_seconds_per_month === null) return undefined;
  return Math.max(0, limits.call_seconds_per_month * 1000 - callMsThisMonth(db));
}

export function checkCall(db: Db, limits: Limits): void {
  const left = callMsLeft(db, limits);
  if (left !== undefined && left < 1000) {
    throw quotaExceeded(`Your plan's ${Math.round(limits.call_seconds_per_month! / 60)} call minutes for this month are used up`);
  }
}
