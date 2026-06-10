// deploy: v3 (2026-06-10) — BACKLOG #19: recovery.updated id is the SLEEP UUID in v2
// (was treated as a cycle id → GET /v2/cycle/{uuid} 404'd on EVERY recovery event, 110
// failed runs/week, recovery_landed pushes never fired). Resolve via the /v2/recovery
// collection (keyed sleep_id) → fetch its integer cycle_id. Also fixes the reversed
// recovery-for-cycle path: /v2/recovery/cycle/{id} (404) → /v2/cycle/{id}/recovery.
// whoop-webhook — WHOOP pushes {type,id,user_id}; we verify the signature, map the
// user to a profile, fetch the referenced record, and upsert on the confirmed key
// (profile_id, whoop_id) for ALL THREE tables (008b/008e). Logs to wearable_sync_log.
// Phase 2 (030): short push notifications after each upsert + dead-man heartbeat check.
// Deploy: supabase functions deploy whoop-webhook --no-verify-jwt
// Register URL in WHOOP dashboard:
//   https://dsnydskkjwziynwmzfkh.supabase.co/functions/v1/whoop-webhook
import { getValidAccessToken, whoopGet, svc } from "../_shared/whoop.ts";

const enc = new TextEncoder();
const b64 = (buf: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(buf)));

// WHOOP signs: base64( HMAC-SHA256( clientSecret, timestamp + rawBody ) )
async function verify(rawBody: string, sig: string | null, ts: string | null): Promise<boolean> {
  if (!sig || !ts) return false;
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(Deno.env.get("WHOOP_CLIENT_SECRET")!),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(ts + rawBody));
  const expected = b64(mac);
  // constant-time-ish compare
  if (expected.length !== sig.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ sig.charCodeAt(i);
  return diff === 0;
}

const msToSec = (ms: number | null | undefined) => ms == null ? null : Math.round(ms / 1000);
const msToMin = (ms: number | null | undefined) => ms == null ? null : Math.round(ms / 600) / 100;
const kjToCal = (kj: number | null | undefined) => kj == null ? null : Math.round(kj / 4.184);

function mapWorkout(rec: any, profileId: string) {
  const s = rec.score ?? {}; const z = s.zone_durations ?? {};
  const total = ["zone_zero_milli","zone_one_milli","zone_two_milli","zone_three_milli","zone_four_milli","zone_five_milli"]
    .reduce((a,k)=>a+(z[k]??0),0);
  // duration from start/end — the Python sync does this (_duration_min); the webhook didn't,
  // so webhook-sourced workouts had duration_min = NULL despite valid timestamps.
  const durationMin = (rec.start && rec.end)
    ? Math.round((new Date(rec.end).getTime() - new Date(rec.start).getTime()) / 60000 * 10) / 10
    : null;
  const row: Record<string, unknown> = {
    profile_id: profileId, whoop_id: String(rec.id),
    cycle_start: rec.start, workout_start: rec.start, workout_end: rec.end,
    timezone: rec.timezone_offset, activity_name: rec.sport_name ?? "Activity",
    duration_min: durationMin,
    activity_strain: s.strain, avg_hr_bpm: s.average_heart_rate, max_hr_bpm: s.max_heart_rate,
    energy_burned_cal: kjToCal(s.kilojoule), source_file: "whoop_webhook",
  };
  if (total > 0) {
    const pct = (ms: number) => Math.round((ms / total) * 10000) / 100;
    row.hr_zone0_sec = msToSec(z.zone_zero_milli); row.hr_zone1_sec = msToSec(z.zone_one_milli);
    row.hr_zone2_sec = msToSec(z.zone_two_milli);  row.hr_zone3_sec = msToSec(z.zone_three_milli);
    row.hr_zone4_sec = msToSec(z.zone_four_milli); row.hr_zone5_sec = msToSec(z.zone_five_milli);
    row.hr_zone0_pct = pct(z.zone_zero_milli ?? 0); row.hr_zone1_pct = pct(z.zone_one_milli ?? 0);
    row.hr_zone2_pct = pct(z.zone_two_milli ?? 0);  row.hr_zone3_pct = pct(z.zone_three_milli ?? 0);
    row.hr_zone4_pct = pct(z.zone_four_milli ?? 0); row.hr_zone5_pct = pct(z.zone_five_milli ?? 0);
  }
  return row;
}

function mapSleep(rec: any, profileId: string) {
  const s = rec.score ?? {}; const st = s.stage_summary ?? {};
  const asleep = (st.total_light_sleep_time_milli??0)+(st.total_slow_wave_sleep_time_milli??0)+(st.total_rem_sleep_time_milli??0);
  // time in bed from start/end (the webhook didn't compute it → in_bed_duration_min was NULL)
  const inBedMin = (rec.start && rec.end)
    ? Math.round((new Date(rec.end).getTime() - new Date(rec.start).getTime()) / 60000 * 10) / 10
    : null;
  return {
    profile_id: profileId, whoop_id: String(rec.id),
    cycle_start: rec.start, sleep_onset: rec.start, wake_onset: rec.end,
    timezone: rec.timezone_offset, is_nap: rec.nap ?? false,
    sleep_performance_pct: s.sleep_performance_percentage,
    sleep_efficiency_pct: s.sleep_efficiency_percentage,
    sleep_consistency_pct: s.sleep_consistency_percentage,
    respiratory_rate_rpm: s.respiratory_rate,
    asleep_duration_min: asleep ? msToMin(asleep) : null,
    in_bed_duration_min: inBedMin,
    deep_sws_min: msToMin(st.total_slow_wave_sleep_time_milli),
    rem_min: msToMin(st.total_rem_sleep_time_milli),
    light_sleep_min: msToMin(st.total_light_sleep_time_milli),
    awake_min: msToMin(st.total_awake_time_milli),
    sleep_need_min: msToMin(s.sleep_needed?.baseline_milli),
    sleep_debt_min: msToMin(s.sleep_needed?.need_from_sleep_debt_milli),
    source_file: "whoop_webhook",
  };
}

// On a sleep event the PRIOR WHOOP cycle has just closed, but WHOOP emits no
// cycle.updated webhook — so the recovery-time cycle row is stale at ~0 day_strain.
// Re-fetch the most-recent CLOSED cycle and upsert its FINAL strain. Best-effort: the
// caller swallows failures so a refresh hiccup never loses the sleep that did save.
async function refreshPriorCycle(db: any, token: string, profileId: string): Promise<number> {
  const col = await whoopGet(token, "/v2/cycle?limit=2");   // most-recent first
  const cycles: any[] = col?.records ?? [];
  // the just-closed cycle is the most recent one that has an `end` (the current cycle is open → end null)
  const prior = cycles.find((c) => c?.end);
  if (!prior) return 0;
  let recov = null; try { recov = await whoopGet(token, `/v2/cycle/${prior.id}/recovery`); } catch (_) {}
  const row = mapCycle(prior, recov, profileId);
  const { error } = await db.from("whoop_cycles").upsert(row, { onConflict: "profile_id,whoop_id" });
  if (error) throw new Error(`prior-cycle refresh: ${error.message}`);
  return 1;
}

function mapCycle(cyc: any, rec: any, profileId: string) {
  const cs = cyc.score ?? {}; const rs = rec?.score ?? {};
  return {
    profile_id: profileId, whoop_id: String(cyc.id),
    cycle_start: cyc.start, cycle_end: cyc.end, timezone: cyc.timezone_offset,
    day_strain: cs.strain, energy_burned_cal: kjToCal(cs.kilojoule),
    avg_hr_bpm: cs.average_heart_rate, max_hr_bpm: cs.max_heart_rate,
    recovery_score_pct: rs.recovery_score, resting_hr_bpm: rs.resting_heart_rate,
    hrv_ms: rs.hrv_rmssd_milli, blood_oxygen_pct: rs.spo2_percentage,
    skin_temp_celsius: rs.skin_temp_celsius, source_file: "whoop_webhook",
  };
}

// ── Telegram send helper ───────────────────────────────────────────────────────
async function telegramSend(chatId: number, text: string): Promise<void> {
  const token = Deno.env.get("TELEGRAM_BOT_TOKEN")!;
  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  }).catch(() => {}); // best-effort — a send failure must not kill the upsert
}

// ── Push config ────────────────────────────────────────────────────────────────

interface PushConfig {
  debounceWindowMin: number;  // minutes; from push.debounce_window_min
  quietStart: number;         // local hour; from push.quiet_hours_start
  quietEnd: number;           // local hour; from push.quiet_hours_end
  recoveryThreshold: number;  // %; from push.recovery_critical_threshold
  hrvCrashPct: number;        // %; from push.hrv_crash_pct
  materialChangePct: number;  // ppt; from push.material_change_pct
  deadManHours: number;       // h; from push.dead_man_hours
}

async function loadPushConfig(db: any): Promise<PushConfig> {
  const { data } = await db
    .from("system_config")
    .select("key, value")
    .in("key", [
      "push.debounce_window_min", "push.quiet_hours_start", "push.quiet_hours_end",
      "push.recovery_critical_threshold", "push.hrv_crash_pct",
      "push.material_change_pct", "push.dead_man_hours",
    ])
    .eq("is_active", true);
  const m: Record<string, number> = {};
  for (const r of data ?? []) m[r.key] = Number(r.value);
  return {
    debounceWindowMin:  m["push.debounce_window_min"]          ?? 30,
    quietStart:         m["push.quiet_hours_start"]             ?? 22,
    quietEnd:           m["push.quiet_hours_end"]               ?? 7,
    recoveryThreshold:  m["push.recovery_critical_threshold"]   ?? 34,
    hrvCrashPct:        m["push.hrv_crash_pct"]                 ?? 20,
    materialChangePct:  m["push.material_change_pct"]           ?? 5,
    deadManHours:       m["push.dead_man_hours"]                ?? 36,
  };
}

// ── Exported pure helpers (tested in webhook_test.ts) ─────────────────────────

/** Convert a UTC timestamp to the local hour using a WHOOP timezone string.
 *  Accepts IANA names ("Asia/Kolkata") and UTC offset strings ("+05:30", "-05:00").
 *  Falls back to UTC hour on unknown/null timezone. */
export function localHour(timezone: string | null, now: Date): number {
  if (!timezone) return now.getUTCHours();
  try {
    const fmt = new Intl.DateTimeFormat("en-US", {
      hour: "numeric", hour12: false, timeZone: timezone,
    });
    const parts = fmt.formatToParts(now);
    const h = parts.find((p) => p.type === "hour");
    if (h) return parseInt(h.value, 10) % 24; // 24 → midnight in some locales
  } catch (_) {}
  // UTC offset fallback: "+05:30", "-05:00", "+5"
  const m = timezone.match(/^([+-])(\d{1,2})(?::(\d{2}))?$/);
  if (m) {
    const sign = m[1] === "+" ? 1 : -1;
    const offsetMin = sign * (parseInt(m[2], 10) * 60 + parseInt(m[3] ?? "0", 10));
    const localMin = (now.getUTCHours() * 60 + now.getUTCMinutes() + offsetMin + 1440) % 1440;
    return Math.floor(localMin / 60);
  }
  return now.getUTCHours();
}

/** Pure threshold check for dead-man alert. Returns true when data is overdue.
 *  lastDataMs: epoch ms of last records_upserted > 0 row; null = never had data. */
export function deadManThreshold(
  lastDataMs: number | null,
  thresholdHours: number,
  nowMs: number,
): boolean {
  if (lastDataMs === null) return true;
  return (nowMs - lastDataMs) / 3600000 >= thresholdHours;
}

/** Decide whether to send a push or suppress it.
 *
 *  Priority order (highest first):
 *    1. Critical → always send (bypasses quiet hours + debounce).
 *    2. Debounce window + material-change gate.
 *    3. Quiet hours (evaluated in local time).
 *    4. Send.
 */
export function decidePush(opts: {
  now: Date;
  lastPush: { sent_at: string; dedup_value: number | null } | null;
  isCritical: boolean;
  currentValue: number | null;
  timezone: string | null;
  config: PushConfig;
}): "send" | "suppress_debounce" | "suppress_quiet" | "suppress_unchanged" {
  const { now, lastPush, isCritical, currentValue, timezone, config } = opts;

  if (isCritical) return "send";

  if (lastPush) {
    const msSinceLast = now.getTime() - new Date(lastPush.sent_at).getTime();
    if (msSinceLast < config.debounceWindowMin * 60000) {
      // Inside debounce window — allow through only if materially changed
      if (currentValue !== null && lastPush.dedup_value !== null) {
        if (Math.abs(currentValue - lastPush.dedup_value) >= config.materialChangePct) {
          return "send";
        }
      }
      return "suppress_debounce";
    }
  }

  // Quiet hours — evaluated in the recipient's local timezone
  const hour = localHour(timezone, now);
  const { quietStart, quietEnd } = config;
  // quietStart > quietEnd means the window wraps midnight (e.g. 22 → 07)
  const inQuiet = quietStart > quietEnd
    ? hour >= quietStart || hour < quietEnd
    : hour >= quietStart && hour < quietEnd;
  if (inQuiet) return "suppress_quiet";

  return "send";
}

/** Compose a short templated Telegram message for the given push type.
 *  Minor framing: performance/growth language; no deficit/restriction words. */
export function composePush(
  pushType: string,
  row: Record<string, unknown>,
  isMinor: boolean,
): string {
  if (pushType === "recovery_landed") {
    const score = row.recovery_score_pct ?? "?";
    const hrv   = row.hrv_ms != null ? `${row.hrv_ms}ms` : "?";
    const rhr   = row.resting_hr_bpm ?? "?";
    return isMinor
      ? `⚡ Recovery ${score}% — HRV ${hrv}, RHR ${rhr}bpm`
      : `📊 Recovery ${score}% | HRV ${hrv} | RHR ${rhr}bpm`;
  }
  if (pushType === "workout_logged") {
    const name   = String(row.activity_name ?? "Workout");
    const strain = row.activity_strain ?? "?";
    const dur    = row.duration_min ? ` · ${row.duration_min}min` : "";
    return isMinor
      ? `💪 ${name}${dur} · Strain ${strain}`
      : `🏋️ ${name}${dur} · Strain ${strain}`;
  }
  return `📌 ${pushType}`;
}

// ── HRV crash detection ────────────────────────────────────────────────────────

async function isHrvCrash(
  db: any,
  profileId: string,
  currentHrv: number,
  crashPct: number,
): Promise<boolean> {
  const { data } = await db
    .from("whoop_cycles")
    .select("hrv_ms")
    .eq("profile_id", profileId)
    .not("hrv_ms", "is", null)
    .order("cycle_start", { ascending: false })
    .limit(7);
  if (!data?.length) return false;
  const avg = data.reduce((s: number, r: any) => s + Number(r.hrv_ms), 0) / data.length;
  if (avg <= 0) return false;
  return ((avg - currentHrv) / avg) * 100 >= crashPct;
}

// ── Dead-man heartbeat ─────────────────────────────────────────────────────────
// Runs on every WHOOP event, checking ALL active profiles (not just the event's
// profile). If a profile has had no WHOOP data in dead_man_hours, an alert is
// pushed to PC. Suppressed for 22h after each alert to avoid spam.
//
// IMPORTANT: this only fires when at least one WHOOP event comes in. For a
// scenario where ALL profiles are simultaneously dead (no events at all), a
// scheduled pg_cron job calling this endpoint is required as a backstop.
async function checkDeadMan(db: any, config: PushConfig): Promise<void> {
  const { data: identities } = await db
    .from("telegram_identities")
    .select("profile_id, display_name")
    .eq("status", "active");
  if (!identities?.length) return;

  // Maintainer's chat_id for alert delivery
  const { data: maint } = await db
    .from("profiles").select("id").eq("is_maintainer", true).single();
  if (!maint) return;
  const { data: maintChat } = await db
    .from("telegram_identities").select("chat_id")
    .eq("profile_id", maint.id).eq("status", "active").maybeSingle();
  if (!maintChat?.chat_id) return;

  const nowMs = Date.now();
  const suppressMs = 22 * 3600000; // re-alert at most once per 22h per profile

  for (const identity of identities) {
    const { data: lastSync } = await db
      .from("wearable_sync_log")
      .select("started_at")
      .eq("profile_id", identity.profile_id)
      .gt("records_upserted", 0)
      .order("started_at", { ascending: false })
      .limit(1)
      .maybeSingle();

    const lastDataMs = lastSync ? new Date(lastSync.started_at).getTime() : null;
    if (!deadManThreshold(lastDataMs, config.deadManHours, nowMs)) continue;

    // Suppress if we already alerted within 22h
    const { data: recentAlert } = await db
      .from("push_log")
      .select("id")
      .eq("profile_id", identity.profile_id)
      .eq("push_type", "dead_man")
      .gte("sent_at", new Date(nowMs - suppressMs).toISOString())
      .limit(1)
      .maybeSingle();
    if (recentAlert) continue;

    const hoursAgo = lastDataMs
      ? Math.round((nowMs - lastDataMs) / 3600000)
      : config.deadManHours;
    const name = identity.display_name ?? identity.profile_id;
    await telegramSend(
      maintChat.chat_id,
      `⚠️ Dead-man: no WHOOP data from ${name} in ${hoursAgo}h`,
    );
    await db.from("push_log").insert({
      profile_id: identity.profile_id,
      push_type: "dead_man",
      subject_id: null,
      status: "sent",
      dedup_value: null,
    });
  }
}

// ── Main handler ───────────────────────────────────────────────────────────────

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });
  const raw = await req.text();
  const ok = await verify(raw, req.headers.get("X-WHOOP-Signature"), req.headers.get("X-WHOOP-Signature-Timestamp"));
  if (!ok) return new Response("bad signature", { status: 401 });

  let evt: any;
  try { evt = JSON.parse(raw); } catch { return new Response("bad json", { status: 400 }); }
  const { type, id, user_id } = evt;
  const db = svc();

  // map WHOOP user_id → profile_id
  const { data: tok } = await db.from("whoop_tokens").select("profile_id").eq("whoop_user_id", String(user_id)).single();
  if (!tok) return new Response("unknown user", { status: 202 });   // 202: accepted, nothing to do
  const profileId = tok.profile_id;

  // open a sync_log run
  const { data: log } = await db.from("wearable_sync_log").insert({
    provider: "whoop", method: "api", sync_type: `webhook:${type}`, status: "in_progress",
    profile_id: profileId, source_path: `${type}/${id}`, started_at: new Date().toISOString(),
    records_in: 1, records_upserted: 0, records_skipped: 0, records_failed: 0,
  }).select("id").single();
  const logId = log?.id;

  try {
    const token = await getValidAccessToken(db, profileId);

    // Handle *.deleted events before routing by prefix — calling whoopGet()
    // on a deleted resource returns 404, causes a 500, and triggers WHOOP retries.
    if (type?.endsWith(".deleted")) {
      // Acknowledge the deletion; soft-delete or mark is a future enhancement.
      // For now, log success so WHOOP does not retry.
      await db.from("wearable_sync_log").update({
        status: "success", completed_at: new Date().toISOString(),
      }).eq("id", logId);
      return new Response("deleted event acknowledged", { status: 200 });
    }

    let table = "", row: Record<string, unknown> = {};
    if (type?.startsWith("workout")) {
      table = "whoop_workouts"; row = mapWorkout(await whoopGet(token, `/v2/activity/workout/${id}`), profileId);
    } else if (type?.startsWith("sleep")) {
      table = "whoop_sleeps"; row = mapSleep(await whoopGet(token, `/v2/activity/sleep/${id}`), profileId);
    } else if (type?.startsWith("recovery")) {
      // v2 recovery webhooks carry the UUID of the ASSOCIATED SLEEP, not a cycle id
      // (v1 sent cycle ids). Find the recovery in the recent collection by sleep_id,
      // then fetch its integer cycle_id. Verified live 2026-06-10.
      const col = await whoopGet(token, "/v2/recovery?limit=10");
      const recov = (col?.records ?? []).find((r: any) => String(r?.sleep_id) === String(id));
      if (!recov) {
        // Not in the recent window (rare — e.g. an old sleep edit). Ack so WHOOP
        // doesn't retry-storm; the nightly sync / refresh_recent picks it up.
        await db.from("wearable_sync_log").update({
          status: "success", records_skipped: 1, completed_at: new Date().toISOString(),
        }).eq("id", logId);
        return new Response("recovery not in recent collection — acknowledged", { status: 200 });
      }
      const cyc = await whoopGet(token, `/v2/cycle/${recov.cycle_id}`);
      table = "whoop_cycles"; row = mapCycle(cyc, recov, profileId);
    } else if (type?.startsWith("cycle")) {
      // WHOOP emits no cycle webhooks in v2 today; if one ever arrives, its id is the
      // integer cycle id (unlike recovery events).
      const cyc = await whoopGet(token, `/v2/cycle/${id}`);
      let recov = null; try { recov = await whoopGet(token, `/v2/cycle/${id}/recovery`); } catch (_) {}
      table = "whoop_cycles"; row = mapCycle(cyc, recov, profileId);
    } else {
      await db.from("wearable_sync_log").update({ status: "success", completed_at: new Date().toISOString() }).eq("id", logId);
      return new Response("ignored type", { status: 202 });
    }

    const { error } = await db.from(table).upsert(row, { onConflict: "profile_id,whoop_id" });
    if (error) throw new Error(`${table} upsert: ${error.message}`);

    // sleep.updated (waking) means the prior cycle just closed → refresh its final strain.
    // Best-effort: the sleep already saved; a refresh failure is logged, not fatal.
    let extra = 0;
    if (type?.startsWith("sleep")) {
      try {
        extra = await refreshPriorCycle(db, token, profileId);
      } catch (e) {
        await db.from("wearable_sync_errors").insert({
          sync_log_id: logId, record_ref: `prior-cycle-after/${type}/${id}`,
          error_code: "prior_cycle_refresh", error_message: (e as Error).message,
        });
      }
    }

    await db.from("wearable_sync_log").update({
      status: "success", records_upserted: 1 + extra, completed_at: new Date().toISOString(),
    }).eq("id", logId);

    // ── Phase 2: push notification (best-effort — never fails the 200 response) ──
    // EdgeRuntime.waitUntil keeps the isolate alive until the promise settles without
    // delaying the 200. The typeof guard lets local/test environments (where
    // EdgeRuntime is undefined) still await the promise rather than silently dropping it.
    const pushTask = (async () => {
      try {
        const cfg = await loadPushConfig(db);
        const { data: identity } = await db
          .from("telegram_identities")
          .select("chat_id, is_minor")
          .eq("profile_id", profileId)
          .eq("status", "active")
          .maybeSingle();

        if (identity?.chat_id) {
          let pushType: string | null = null;
          let dedupValue: number | null = null;
          let isCritical = false;

          if (type?.startsWith("recovery") || type?.startsWith("cycle")) {
            pushType = "recovery_landed";
            dedupValue = typeof row.recovery_score_pct === "number" ? row.recovery_score_pct : null;
            isCritical = dedupValue !== null && dedupValue < cfg.recoveryThreshold;
            if (!isCritical && typeof row.hrv_ms === "number" && (row.hrv_ms as number) > 0) {
              isCritical = await isHrvCrash(db, profileId, row.hrv_ms as number, cfg.hrvCrashPct);
            }
          } else if (type?.startsWith("workout")) {
            pushType = "workout_logged";
            dedupValue = typeof row.activity_strain === "number" ? row.activity_strain : null;
          }

          if (pushType) {
            const subjectId = String(id);
            const { data: lastPush } = await db
              .from("push_log")
              .select("sent_at, dedup_value")
              .eq("profile_id", profileId)
              .eq("push_type", pushType)
              .eq("subject_id", subjectId)
              .order("sent_at", { ascending: false })
              .limit(1)
              .maybeSingle();

            const timezone = typeof row.timezone === "string" ? row.timezone : null;
            const decision = decidePush({
              now: new Date(),
              lastPush,
              isCritical,
              currentValue: dedupValue,
              timezone,
              config: cfg,
            });

            if (decision === "send") {
              await telegramSend(
                identity.chat_id,
                composePush(pushType, row, identity.is_minor ?? false),
              );
            }
            // Log every attempt (sent or suppressed) — idempotency + audit trail
            await db.from("push_log").insert({
              profile_id: profileId,
              push_type: pushType,
              subject_id: subjectId,
              status: decision === "send" ? "sent" : "suppressed",
              dedup_value: dedupValue,
            });
          }
        }

        // Dead-man heartbeat: check all active profiles on every WHOOP event
        await checkDeadMan(db, cfg);
      } catch (_) {
        // Push errors are non-fatal; upsert already succeeded
      }
    })();

    // Register with EdgeRuntime so the isolate is not reclaimed before pushTask settles.
    // Falls back to awaiting directly in local/test environments where EdgeRuntime is absent.
    if (typeof EdgeRuntime !== "undefined") {
      EdgeRuntime.waitUntil(pushTask);
    } else {
      await pushTask;
    }

    return new Response("ok", { status: 200 });
  } catch (e) {
    const msg = (e as Error).message;
    if (logId) {
      await db.from("wearable_sync_log").update({ status: "failed", records_failed: 1, completed_at: new Date().toISOString() }).eq("id", logId);
      await db.from("wearable_sync_errors").insert({ sync_log_id: logId, record_ref: `${type}/${id}`, error_code: "webhook", error_message: msg });
    }
    return new Response(`error: ${msg}`, { status: 500 });
  }
});
