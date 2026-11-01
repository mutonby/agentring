import { describe, expect, it } from "vitest";
import { openDatabase } from "../src/db/database.js";
import { callMsLeft, callMsThisMonth, monthStart } from "../src/quota.js";

const NOV = Date.UTC(2026, 10, 1); // 2026-11-01T00:00Z

function dbWith(calls: Array<[answered: number, ended: number | null]>) {
  const db = openDatabase(":memory:");
  calls.forEach(([a, e], i) =>
    db
      .prepare(
        `INSERT INTO calls (call_id, direction, state, priority, agent_name, created_at, expires_at, answered_at, ended_at)
         VALUES (?, 'agent_to_user', ?, 'normal', 'A', ?, ?, ?, ?)`,
      )
      .run(`call_${i}`, e === null ? "active" : "ended", a, a + 30_000, a, e),
  );
  return db;
}

describe("call quota accounting", () => {
  it("counts only this month's part of a call that crosses the month boundary", () => {
    const now = NOV + 30 * 60_000; // Nov 1 00:30
    const db = dbWith([
      [NOV - 10 * 60_000, NOV + 20 * 60_000], // Oct 31 23:50 → Nov 1 00:20: 20 min in November
      [NOV - 60 * 60_000, NOV - 50 * 60_000], // entirely in October
      [NOV + 25 * 60_000, null], // active since 00:25: 5 min so far
    ]);
    expect(monthStart(now)).toBe(NOV);
    expect(callMsThisMonth(db, now)).toBe(25 * 60_000);
  });

  it("counts an active call that started last month up to now", () => {
    const db = dbWith([[NOV - 5 * 60_000, null]]);
    expect(callMsThisMonth(db, NOV + 7 * 60_000)).toBe(7 * 60_000);
  });

  it("is unlimited without a limit", () => {
    expect(callMsLeft(dbWith([]), { max_devices: null, call_seconds_per_month: null, messages_per_day: null })).toBeUndefined();
  });
});
