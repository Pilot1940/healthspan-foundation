// whoop-webhook — WHOOP pushes {type,id,user_id}; we verify the signature, map the
// user to a profile, fetch the referenced record, and upsert on the confirmed key
// (profile_id, whoop_id) for ALL THREE tables (008b/008e). Logs to wearable_sync_log.
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
  const row: Record<string, unknown> = {
    profile_id: profileId, whoop_id: String(rec.id),
    cycle_start: rec.start, workout_start: rec.start, workout_end: rec.end,
    timezone: rec.timezone_offset, activity_name: rec.sport_name ?? "Activity",
    activity_strain: s.strain, avg_hr_bpm: s.average_heart_rate, max_hr_bpm: s.max_heart_rate,
    energy_burned_cal: kjToCal(s.kilojoule), source_file: "whoop_webhook",
  };
  if (total > 0) {
    row.hr_zone0_sec = msToSec(z.zone_zero_milli); row.hr_zone1_sec = msToSec(z.zone_one_milli);
    row.hr_zone2_sec = msToSec(z.zone_two_milli);  row.hr_zone3_sec = msToSec(z.zone_three_milli);
    row.hr_zone4_sec = msToSec(z.zone_four_milli); row.hr_zone5_sec = msToSec(z.zone_five_milli);
  }
  return row;
}

function mapSleep(rec: any, profileId: string) {
  const s = rec.score ?? {}; const st = s.stage_summary ?? {};
  const asleep = (st.total_light_sleep_time_milli??0)+(st.total_slow_wave_sleep_time_milli??0)+(st.total_rem_sleep_time_milli??0);
  return {
    profile_id: profileId, whoop_id: String(rec.id),
    cycle_start: rec.start, sleep_onset: rec.start, wake_onset: rec.end,
    timezone: rec.timezone_offset, is_nap: rec.nap ?? false,
    sleep_performance_pct: s.sleep_performance_percentage,
    sleep_efficiency_pct: s.sleep_efficiency_percentage,
    respiratory_rate_rpm: s.respiratory_rate,
    asleep_duration_min: asleep ? msToMin(asleep) : null,
    deep_sws_min: msToMin(st.total_slow_wave_sleep_time_milli),
    rem_min: msToMin(st.total_rem_sleep_time_milli),
    light_sleep_min: msToMin(st.total_light_sleep_time_milli),
    source_file: "whoop_webhook",
  };
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
    let table = "", row: Record<string, unknown> = {};
    if (type?.startsWith("workout")) {
      table = "whoop_workouts"; row = mapWorkout(await whoopGet(token, `/v2/activity/workout/${id}`), profileId);
    } else if (type?.startsWith("sleep")) {
      table = "whoop_sleeps"; row = mapSleep(await whoopGet(token, `/v2/activity/sleep/${id}`), profileId);
    } else if (type?.startsWith("recovery") || type?.startsWith("cycle")) {
      const cyc = await whoopGet(token, `/v2/cycle/${id}`);
      let recov = null; try { recov = await whoopGet(token, `/v2/recovery/cycle/${id}`); } catch (_) {}
      table = "whoop_cycles"; row = mapCycle(cyc, recov, profileId);
    } else {
      await db.from("wearable_sync_log").update({ status: "success", completed_at: new Date().toISOString() }).eq("id", logId);
      return new Response("ignored type", { status: 202 });
    }

    const { error } = await db.from(table).upsert(row, { onConflict: "profile_id,whoop_id" });
    if (error) throw new Error(`${table} upsert: ${error.message}`);

    await db.from("wearable_sync_log").update({
      status: "success", records_upserted: 1, completed_at: new Date().toISOString(),
    }).eq("id", logId);
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
