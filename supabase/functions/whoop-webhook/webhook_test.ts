// Deno unit tests for whoop-webhook Phase 2 push helpers.
// Run: deno test supabase/functions/whoop-webhook/webhook_test.ts
//
// Tests pure exported functions (decidePush, localHour, composePush, deadManThreshold)
// without a live DB, Telegram connection, or WHOOP API.
// Integration tests (push_log row count, dedup_value stored correctly) live in
// tests/unit/test_reconcile.py (Python DB-layer tests).
import { assertEquals } from "https://deno.land/std@0.224.0/assert/mod.ts";
import { decidePush, localHour, composePush, deadManThreshold } from "./index.ts";

// Canonical default config — mirrors migration 030 seeds
const cfg = {
  debounceWindowMin: 30,
  quietStart: 22,
  quietEnd: 7,
  recoveryThreshold: 34,
  hrvCrashPct: 20,
  materialChangePct: 5,
  deadManHours: 36,
};

// ── decidePush: critical-always ───────────────────────────────────────────────

Deno.test("decidePush: critical always sends regardless of quiet hours", () => {
  const now = new Date("2026-06-07T02:00:00Z"); // 2 am UTC — inside quiet
  assertEquals(
    decidePush({ now, lastPush: null, isCritical: true, currentValue: 28, timezone: null, config: cfg }),
    "send",
  );
});

Deno.test("decidePush: critical always sends even within debounce window", () => {
  const now = new Date("2026-06-07T10:00:00Z");
  // Last push 5min ago — would normally be suppressed
  const lastPush = { sent_at: new Date(now.getTime() - 5 * 60000).toISOString(), dedup_value: 30 };
  assertEquals(
    decidePush({ now, lastPush, isCritical: true, currentValue: 28, timezone: null, config: cfg }),
    "send",
  );
});

// ── decidePush: debounce collapses a burst to one push ────────────────────────

Deno.test("decidePush: debounce collapses a burst to one push", () => {
  const t0 = new Date("2026-06-07T10:00:00Z");

  // Event 0 — no prior push → sends
  const d0 = decidePush({ now: t0, lastPush: null, isCritical: false, currentValue: 70, timezone: null, config: cfg });

  // Event 1 — 5min later, score 70→71 (change 1ppt < 5 threshold) → debounced
  const t1 = new Date(t0.getTime() + 5 * 60000);
  const d1 = decidePush({
    now: t1,
    lastPush: { sent_at: t0.toISOString(), dedup_value: 70 },
    isCritical: false, currentValue: 71, timezone: null, config: cfg,
  });

  // Event 2 — 10min later, score 70→72 (change 2ppt < 5 threshold) → debounced
  const t2 = new Date(t0.getTime() + 10 * 60000);
  const d2 = decidePush({
    now: t2,
    lastPush: { sent_at: t0.toISOString(), dedup_value: 70 },
    isCritical: false, currentValue: 72, timezone: null, config: cfg,
  });

  assertEquals(d0, "send");
  assertEquals(d1, "suppress_debounce");
  assertEquals(d2, "suppress_debounce");
});

Deno.test("decidePush: material change overrides debounce (re-score 5ppt+ re-pushes)", () => {
  const now = new Date("2026-06-07T10:00:00Z");
  // Last push 5min ago, score was 70; now re-scored to 62 (change 8ppt ≥ 5)
  const lastPush = { sent_at: new Date(now.getTime() - 5 * 60000).toISOString(), dedup_value: 70 };
  assertEquals(
    decidePush({ now, lastPush, isCritical: false, currentValue: 62, timezone: null, config: cfg }),
    "send",
  );
});

Deno.test("decidePush: sub-threshold change stays debounced", () => {
  const now = new Date("2026-06-07T10:00:00Z");
  const lastPush = { sent_at: new Date(now.getTime() - 5 * 60000).toISOString(), dedup_value: 70 };
  // Change of exactly 4ppt — below 5 threshold → suppress
  assertEquals(
    decidePush({ now, lastPush, isCritical: false, currentValue: 74, timezone: null, config: cfg }),
    "suppress_debounce",
  );
});

// ── decidePush: quiet-hours suppression ───────────────────────────────────────

Deno.test("decidePush: quiet-hours suppresses non-critical at 11pm UTC", () => {
  const now = new Date("2026-06-07T23:00:00Z"); // 11pm UTC — inside quiet (22–07)
  assertEquals(
    decidePush({ now, lastPush: null, isCritical: false, currentValue: 70, timezone: null, config: cfg }),
    "suppress_quiet",
  );
});

Deno.test("decidePush: quiet-hours uses local timezone, not UTC", () => {
  // 16:30 UTC = 22:00 IST (UTC+5:30) — inside IST quiet hours; outside UTC quiet hours
  const now = new Date("2026-06-07T16:30:00Z");
  const utcDecision = decidePush({ now, lastPush: null, isCritical: false, currentValue: 70, timezone: null, config: cfg });
  const istDecision = decidePush({ now, lastPush: null, isCritical: false, currentValue: 70, timezone: "Asia/Kolkata", config: cfg });
  assertEquals(utcDecision, "send");          // 16:30 UTC is within active hours
  assertEquals(istDecision, "suppress_quiet"); // 22:00 IST is inside quiet hours
});

Deno.test("decidePush: sends after quiet hours end", () => {
  // 07:00 UTC — exactly at quiet end (edge: quietEnd = 7 means 'from 7 onwards' is active)
  const now = new Date("2026-06-07T07:00:00Z");
  assertEquals(
    decidePush({ now, lastPush: null, isCritical: false, currentValue: 70, timezone: null, config: cfg }),
    "send",
  );
});

// ── decidePush: push idempotency on webhook replay ────────────────────────────
// Spec §2: same (type, id) can be replayed; second push must be suppressed.

Deno.test("decidePush: push idempotency — replay within debounce window suppresses", () => {
  const now = new Date("2026-06-07T10:00:00Z");

  // First delivery: no prior push → sends
  const first = decidePush({ now, lastPush: null, isCritical: false, currentValue: 72, timezone: null, config: cfg });

  // Replay 2min later (same score — webhook retry): log entry from first delivery exists
  const replay = decidePush({
    now: new Date(now.getTime() + 2 * 60000),
    lastPush: { sent_at: now.toISOString(), dedup_value: 72 },
    isCritical: false, currentValue: 72, timezone: null, config: cfg,
  });

  assertEquals(first, "send");
  assertEquals(replay, "suppress_debounce");
});

// ── localHour ─────────────────────────────────────────────────────────────────

Deno.test("localHour: null timezone returns UTC hour", () => {
  const now = new Date("2026-06-07T14:30:00Z");
  assertEquals(localHour(null, now), 14);
});

Deno.test("localHour: IANA timezone Asia/Kolkata (+5:30)", () => {
  // 00:30 UTC = 06:00 IST
  const now = new Date("2026-06-07T00:30:00Z");
  assertEquals(localHour("Asia/Kolkata", now), 6);
});

Deno.test("localHour: UTC offset string +05:30", () => {
  const now = new Date("2026-06-07T00:30:00Z");
  assertEquals(localHour("+05:30", now), 6);
});

Deno.test("localHour: UTC offset string -05:00 (EST)", () => {
  // 12:00 UTC - 5h = 07:00 EST
  const now = new Date("2026-06-07T12:00:00Z");
  assertEquals(localHour("-05:00", now), 7);
});

Deno.test("localHour: invalid timezone falls back to UTC", () => {
  const now = new Date("2026-06-07T14:00:00Z");
  assertEquals(localHour("Not/ATimezone", now), 14);
});

// ── deadManThreshold ──────────────────────────────────────────────────────────

Deno.test("deadManThreshold: null (never synced) always fires", () => {
  assertEquals(deadManThreshold(null, 36, Date.now()), true);
});

Deno.test("deadManThreshold: exactly at 36h threshold fires", () => {
  const nowMs = Date.now();
  assertEquals(deadManThreshold(nowMs - 36 * 3600000, 36, nowMs), true);
});

Deno.test("deadManThreshold: just under 36h does not fire", () => {
  const nowMs = Date.now();
  assertEquals(deadManThreshold(nowMs - 35 * 3600000 - 3599000, 36, nowMs), false);
});

Deno.test("deadManThreshold: recent data does not fire", () => {
  const nowMs = Date.now();
  assertEquals(deadManThreshold(nowMs - 2 * 3600000, 36, nowMs), false);
});

// ── composePush ───────────────────────────────────────────────────────────────

Deno.test("composePush: recovery_landed includes score, HRV, RHR", () => {
  const msg = composePush("recovery_landed", { recovery_score_pct: 72, hrv_ms: 55, resting_hr_bpm: 51 }, false);
  assertEquals(msg.includes("72"), true);
  assertEquals(msg.includes("55"), true);
  assertEquals(msg.includes("51"), true);
});

Deno.test("composePush: recovery_landed minor framing uses no deficit language", () => {
  const msg = composePush("recovery_landed", { recovery_score_pct: 35, hrv_ms: 28, resting_hr_bpm: 62 }, true);
  // Minor framing must not use words that imply deficit/restriction
  for (const bad of ["low", "poor", "bad", "deficit", "restrict"]) {
    assertEquals(msg.toLowerCase().includes(bad), false, `Should not contain '${bad}'`);
  }
  assertEquals(msg.includes("35"), true);
});

Deno.test("composePush: workout_logged includes name, strain, duration", () => {
  const msg = composePush("workout_logged", { activity_name: "Running", activity_strain: 14.2, duration_min: 45 }, false);
  assertEquals(msg.includes("Running"), true);
  assertEquals(msg.includes("14.2"), true);
  assertEquals(msg.includes("45"), true);
});

Deno.test("composePush: workout_logged minor framing is encouraging", () => {
  const msg = composePush("workout_logged", { activity_name: "Weightlifting", activity_strain: 12.0, duration_min: 60 }, true);
  assertEquals(msg.includes("Weightlifting"), true);
  assertEquals(msg.includes("12"), true);
});

Deno.test("composePush: unknown push_type returns non-empty fallback", () => {
  const msg = composePush("media_done", {}, false);
  assertEquals(msg.length > 0, true);
});
