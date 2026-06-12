// deploy: 2026-06-11 v9 — two-level "📝 Update today" menu (training ticks + supplement slots/pills) + /whoop reconnect
// deploy: 2026-06-11 v8 — sprint adherence ticks: callback_query → goals.adherence_log (additive)
// deploy: 2026-06-09 ce91936 — supersede-on-reply (logged food) + retire review row on clarify
// telegram-webhook — Phase 1 + 3A ingestion: auth, dedup, media→Storage, enqueue, Routine fire.
// Phase 3A adds: media_group_id capture (album clustering) + Routine fire-trigger (deduped).
//
// Auth model: service_role (same as whoop-webhook). No user session in an inbound webhook.
// Secrets read: TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET,
//               SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (injected by Supabase runtime).
//
// Idempotency ordering (CRITICAL — spec §2):
//   photo: getFile → upload Storage → INSERT media_inbox → INSERT processed_updates → ack
//   text:  INSERT media_inbox → INSERT processed_updates → ack
// processed_updates row written ONLY after media_inbox succeeds, so a failure before
// that point leaves no row and Telegram's retry re-runs the full path.
//
// Injection rule (spec §5): caption/text is DATA only. guessKind() reads it for
// classification but no code path executes, eval-s, or acts on the text content.
//
// Deploy: supabase functions deploy telegram-webhook --no-verify-jwt
import { createClient, SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2";
import { sendReconnectPrompt } from "../_shared/whoop.ts";

// ── helpers (exported for unit tests) ─────────────────────────────────────────

export function verifySecretToken(header: string | null, expected: string): boolean {
  if (!header || !expected) return false;
  if (header.length !== expected.length) return false;
  // constant-time compare
  let diff = 0;
  for (let i = 0; i < expected.length; i++) {
    diff |= header.charCodeAt(i) ^ expected.charCodeAt(i);
  }
  return diff === 0;
}

// deploy: v6 (2026-06-08) — reply-to-clarify loop (reply to a staged item to fix it)
export function guessKind(caption: string | undefined): "food" | "workout" | "lab" | "dexa" | "unknown" {
  if (!caption) return "unknown";
  const t = caption.toLowerCase();

  // Order matters: check the specific, document/activity-like kinds FIRST, then fall
  // through to a deliberately liberal food net. A genuine lab/workout/dexa caption wins
  // over food; anything edible-looking that isn't one of those becomes food (richer
  // food prompt: ingredient decomposition + nutrition-label reading).
  if (/\b(dexa|dxa|body.?comp|composition|fat.?mass|lean.?mass|bone.?density|bmd)\b/.test(t)) return "dexa";
  if (/\b(lab|labs|blood|bloodwork|test|result|report|hba1c|a1c|cholesterol|ldl|hdl|triglyceride|creatinine|tsh|t3|t4|panel|rbc|wbc|crp|ferritin|insulin|cortisol|testosterone|apob|lipid|cbc|urine|biopsy)\b/.test(t)) return "lab";
  if (/\b(workout|exercise|gym|run|ran|jog|swim|swam|bike|biked|cycle|cycling|ride|lift|lifted|strain|training|trained|sport|hiit|cardio|yoga|pilates|crossfit|tennis|padel|squash|hike|hiked|walk|steps|pushup|pullup|squat|deadlift|bench|row|sprint|marathon|workout)\b/.test(t)) return "workout";

  // FOOD — very liberal. Any food/drink noun or eating verb routes here.
  if (/\b(food|meal|eat|ate|eaten|eating|drink|drank|drunk|drinking|had|having|consume|consumed|nutrition|nutritional|kcal|cal|cals|calorie|calories|macro|macros|protein|carb|carbs|carbohydrate|fat|fats|fiber|fibre|sugar|glucose|creatine|glutamine|breakfast|lunch|dinner|snack|brunch|supper|dessert|appetizer|starter|side)\b/.test(t)) return "food";
  // FOOD nouns / forms — drinks, dishes, packaged items, staples, common foods.
  if (/\b(shake|smoothie|protein.?shake|juice|coffee|espresso|latte|cappuccino|tea|matcha|soda|cola|kombucha|electrolyte|electrolytes|water|milk|yogurt|yoghurt|lassi|kefir|bottle|can|pack|packet|sachet|scoop|serving|bar|powder|cereal|oats|oatmeal|granola|muesli|egg|eggs|omelette|omelet|chicken|beef|pork|lamb|mutton|fish|salmon|tuna|prawn|shrimp|seafood|paneer|tofu|tempeh|rice|quinoa|bread|toast|bagel|roti|naan|chapati|paratha|pasta|noodle|noodles|ramen|pho|salad|soup|stew|curry|dal|daal|dhal|sambar|kebab|kabab|kabob|chello|burger|pizza|sandwich|wrap|roll|taco|burrito|sushi|dumpling|momo|samosa|pakora|fries|chips|fruit|banana|apple|mango|orange|grape|grapes|berry|berries|strawberry|blueberry|avocado|nuts|almond|almonds|peanut|cashew|walnut|cheese|butter|ghee|cream|chocolate|cake|cookie|biscuit|donut|ice.?cream|gelato|pudding|honey|jam|plate|bowl|dish|portion|bite)\b/.test(t)) return "food";

  return "unknown";
}

// ── Supabase client (service_role — bypasses RLS; same pattern as whoop-webhook) ──

function svc(): SupabaseClient {
  return createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    { auth: { persistSession: false } },
  );
}

// ── Telegram helpers ────────────────────────────────────────────────────────────

async function telegramSend(chatId: number, text: string): Promise<void> {
  const token = Deno.env.get("TELEGRAM_BOT_TOKEN")!;
  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  }).catch(() => {}); // best-effort — never let a reply failure lose the update
}

// ── Sprint adherence ticks (inline-keyboard callbacks) ───────────────────────────
// The daily brief attaches an inline keyboard (lib/sprints.adherence_keyboard); each tap
// is a callback_query handled here. Must mirror the Python keyboard layout exactly.

const TICK_ACTIVITIES = ["gym", "beach", "pool", "hike", "massage"];

export function buildAdherenceKeyboard(sprintId: string, dateIso: string, done: Record<string, boolean>) {
  const btn = (a: string) => ({
    text: `${done[a] ? "✅" : "⬜"} ${a}`,
    callback_data: `tick:${sprintId}:${dateIso}:${a}`,
  });
  return {
    inline_keyboard: [
      TICK_ACTIVITIES.slice(0, 3).map(btn),
      TICK_ACTIVITIES.slice(3).map(btn),
    ],
  };
}

async function answerCallback(callbackId: string, text: string): Promise<void> {
  const token = Deno.env.get("TELEGRAM_BOT_TOKEN")!;
  await fetch(`https://api.telegram.org/bot${token}/answerCallbackQuery`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ callback_query_id: callbackId, text }),
  }).catch(() => {});
}

async function editKeyboard(chatId: number, messageId: number, replyMarkup: unknown): Promise<void> {
  const token = Deno.env.get("TELEGRAM_BOT_TOKEN")!;
  await fetch(`https://api.telegram.org/bot${token}/editMessageReplyMarkup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, message_id: messageId, reply_markup: replyMarkup }),
  }).catch(() => {}); // best-effort — the DB write is the source of truth, not the button face
}

// Persist one adherence tick into sprints.goals.adherence_log[date][activity] = true.
// service_role bypasses RLS, so ownership is enforced explicitly (sprint.profile_id ===
// the caller's identity). Returns the day's done-map for the keyboard refresh, or null on
// a rejected/failed tick. Idempotent (set true).
async function applyTick(
  db: SupabaseClient, sprintId: string, dateIso: string, activity: string, profileId: string,
): Promise<Record<string, boolean> | null> {
  if (!TICK_ACTIVITIES.includes(activity)) return null;
  const { data: sprint } = await db
    .from("sprints").select("id, profile_id, goals").eq("id", sprintId).maybeSingle();
  if (!sprint || sprint.profile_id !== profileId) return null;  // ownership check

  // Normalize like lib/sprints.normalize_goals: legacy flat array → {block_goals: [...]}.
  const goals: any = Array.isArray(sprint.goals)
    ? { block_goals: sprint.goals }
    : (sprint.goals && typeof sprint.goals === "object" ? sprint.goals : {});
  const log = (goals.adherence_log && typeof goals.adherence_log === "object") ? goals.adherence_log : {};
  const day = { ...(log[dateIso] ?? {}), [activity]: true };
  const newGoals = { ...goals, adherence_log: { ...log, [dateIso]: day } };

  const { error } = await db.from("sprints").update({ goals: newGoals }).eq("id", sprintId);
  if (error) return null;
  return day;
}

// ── Two-level "📝 Update today" menu: training toggles + supplement slots (option C) ─────
// The brief carries ONE button (menu:<date>); tapping expands to this menu, so the brief
// stays clean no matter how many actions exist. All keyboards are rendered server-side from
// fresh data on each tap (no state in callback_data beyond date + ids).

const SLOT_ORDER = ["morning", "lunch", "dinner", "bedtime", "anytime"];
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

interface SuppItem { id: string; name: string; taken: boolean }

// Fetch everything the menu needs for a profile + day: the active sprint (training toggles)
// and the active supplement regimens grouped by timing slot with today's taken state.
async function getMenuData(db: SupabaseClient, profileId: string, dateIso: string) {
  const { data: sprint } = await db
    .from("sprints").select("id, goals")
    .eq("profile_id", profileId).lte("start_date", dateIso).gte("end_date", dateIso)
    .order("start_date", { ascending: false }).limit(1).maybeSingle();

  const { data: regs } = await db
    .from("supplement_regimens").select("supplement_id, timing")
    .eq("profile_id", profileId).eq("status", "active")
    .lte("start_date", dateIso).or(`end_date.is.null,end_date.gte.${dateIso}`);

  const ids = [...new Set((regs ?? []).map((r: any) => r.supplement_id))];
  const names: Record<string, string> = {};
  if (ids.length) {
    const { data: supps } = await db.from("supplements").select("id, display_name, name").in("id", ids);
    for (const s of supps ?? []) names[s.id] = s.display_name || s.name || s.id;
  }
  // "taken today" = ANY non-voided intake (any source), matching the brief's count.
  const { data: intakes } = await db
    .from("supplement_intake_logs").select("supplement_id")
    .eq("profile_id", profileId).eq("taken_on", dateIso).is("voided_at", null);
  const taken = new Set((intakes ?? []).map((r: any) => r.supplement_id));

  const slots: Record<string, SuppItem[]> = {};
  for (const r of regs ?? []) {
    let timing = (r as any).timing;
    if (typeof timing === "string") timing = [timing];
    if (!Array.isArray(timing) || !timing.length) timing = ["anytime"];
    for (const t of timing) {
      const key = SLOT_ORDER.includes(t) ? t : "anytime";
      (slots[key] ??= []).push({ id: r.supplement_id, name: names[r.supplement_id] ?? r.supplement_id, taken: taken.has(r.supplement_id) });
    }
  }
  return { sprint, slots };
}

export function topMenu(dateIso: string, sprint: any, slots: Record<string, SuppItem[]>) {
  const rows: any[] = [];
  if (sprint?.id) {
    const adher: Record<string, boolean> = sprint.goals?.adherence_log?.[dateIso] ?? {};
    const tbtn = (a: string) => ({ text: `${adher[a] ? "✅" : "⬜"} ${a}`, callback_data: `tick:${sprint.id}:${dateIso}:${a}` });
    rows.push(TICK_ACTIVITIES.slice(0, 3).map(tbtn));
    rows.push(TICK_ACTIVITIES.slice(3).map(tbtn));
  }
  const slotBtns: any[] = [];
  for (const slot of SLOT_ORDER) {
    const items = slots[slot];
    if (!items?.length) continue;
    const done = items.filter((i) => i.taken).length;
    slotBtns.push({ text: `💊 ${cap(slot)} ${done}/${items.length}`, callback_data: `slot:${dateIso}:${slot}` });
  }
  for (let i = 0; i < slotBtns.length; i += 2) rows.push(slotBtns.slice(i, i + 2));
  rows.push([{ text: "✕ close", callback_data: `close:${dateIso}` }]);
  return { inline_keyboard: rows };
}

export function slotMenu(dateIso: string, slot: string, items: SuppItem[]) {
  const rows = items.map((i) => [{ text: `${i.taken ? "✅" : "⬜"} ${i.name}`, callback_data: `supp:${dateIso}:${i.id}` }]);
  rows.push([{ text: "⬅ back", callback_data: `back:${dateIso}` }]);
  return { inline_keyboard: rows };
}

// Toggle a supplement's "taken today" state. Append-only: untake = void today's non-voided
// intakes (any source); take = un-void a prior voided row or insert a fresh telegram one.
// All scoped to profileId (the caller's own profile). Returns the new taken bool.
async function toggleSupplement(db: SupabaseClient, profileId: string, suppId: string, dateIso: string): Promise<boolean> {
  const { data: live } = await db
    .from("supplement_intake_logs").select("id")
    .eq("profile_id", profileId).eq("supplement_id", suppId).eq("taken_on", dateIso).is("voided_at", null);
  if (live?.length) {
    await db.from("supplement_intake_logs")
      .update({ voided_at: new Date().toISOString(), void_reason: "untapped via Telegram" })
      .in("id", live.map((r: any) => r.id));
    return false;
  }
  const { data: voided } = await db
    .from("supplement_intake_logs").select("id")
    .eq("profile_id", profileId).eq("supplement_id", suppId).eq("taken_on", dateIso).not("voided_at", "is", null).limit(1);
  if (voided?.length) {
    await db.from("supplement_intake_logs").update({ voided_at: null, void_reason: null }).eq("id", voided[0].id);
    return true;
  }
  await db.from("supplement_intake_logs")
    .insert({ profile_id: profileId, supplement_id: suppId, taken_at: new Date().toISOString(), source: "telegram" });
  return true;
}

async function alertMaintainer(db: SupabaseClient, unknownChatId: number, preview?: string): Promise<void> {
  // Find the maintainer's active Telegram chat (best-effort; may not exist at bootstrap).
  const { data: profile } = await db
    .from("profiles")
    .select("id")
    .eq("is_maintainer", true)
    .single();
  if (!profile) return;

  const { data: identity } = await db
    .from("telegram_identities")
    .select("chat_id")
    .eq("profile_id", profile.id)
    .eq("status", "active")
    .maybeSingle();
  if (!identity?.chat_id) return;

  const msg = preview
    ? `⚠️ Unknown chat ${unknownChatId} tried to connect. First msg: "${preview.slice(0, 60)}". Mint a link code to onboard them.`
    : `⚠️ Unknown chat ${unknownChatId} tried to connect. Mint a link code to onboard them.`;
  await telegramSend(identity.chat_id, msg);
}

// ── Routine fire-trigger (Phase 3A) ────────────────────────────────────────────
//
// Fires the Routine drain at most once per routine.fire_dedup_sec (default 300s).
// Dedup uses an atomic compare-and-set PATCH on system_config.updated_at so
// concurrent webhook fires cannot double-fire within the window.
// Requires secrets: ROUTINE_TRIGGER_URL, ROUTINE_BEARER (optional).
// Runs inside EdgeRuntime.waitUntil — never delays the 200 response.

async function maybeFireRoutine(db: SupabaseClient): Promise<void> {
  const routineUrl = Deno.env.get("ROUTINE_TRIGGER_URL");
  if (!routineUrl) return;

  // Read dedup window from system_config
  const { data: cfgRows } = await db
    .from("system_config")
    .select("key,value")
    .in("key", ["routine.fire_dedup_sec"])
    .eq("is_active", true);

  const dedupSec = Number(cfgRows?.find((r: any) => r.key === "routine.fire_dedup_sec")?.value ?? 300);
  const nowMs = Date.now();
  const cutoff = new Date(nowMs - dedupSec * 1000).toISOString();
  const nowIso = new Date(nowMs).toISOString();

  // Atomic compare-and-set: update only if last fire was ≥ dedupSec ago.
  // updated_at is timestamptz — reliable comparison; value stores human-readable ISO.
  const { data: won } = await db
    .from("system_config")
    .update({ value: nowIso, updated_at: nowIso })
    .eq("key", "routine.last_fire_at")
    .eq("is_active", true)
    .lt("updated_at", cutoff)
    .select("id");

  if (!won?.length) return; // another fire won the race (or row not found)

  const bearer = Deno.env.get("ROUTINE_BEARER") ?? "";
  await fetch(routineUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(bearer ? { "Authorization": `Bearer ${bearer}` } : {}),
    },
    body: JSON.stringify({ trigger: "telegram_webhook", ts: nowMs }),
  });
}

// ── Telegram file download + Storage upload ─────────────────────────────────────

async function storeMedia(
  db: SupabaseClient,
  fileId: string,
  profileId: string,
  updateId: number,
  mimeType?: string,
): Promise<string | null> {
  const token = Deno.env.get("TELEGRAM_BOT_TOKEN")!;

  // 1. Resolve file_path from Telegram
  const infoResp = await fetch(`https://api.telegram.org/bot${token}/getFile?file_id=${fileId}`);
  const info = await infoResp.json();
  if (!info.ok || !info.result?.file_path) return null;

  const filePath: string = info.result.file_path;
  const ext = filePath.split(".").pop() ?? "bin";

  // 2. Download file bytes
  const fileResp = await fetch(`https://api.telegram.org/file/bot${token}/${filePath}`);
  if (!fileResp.ok) return null;
  const bytes = await fileResp.arrayBuffer();

  // 3. Upload to health-media bucket (private; signed URLs only)
  const objectPath = `telegram/${profileId}/${updateId}.${ext}`;
  const contentType = mimeType ?? (ext === "pdf" ? "application/pdf" : "image/jpeg");

  const { error } = await db.storage
    .from("health-media")
    .upload(objectPath, bytes, { contentType, upsert: false });

  return error ? null : objectPath;
}

// ── Main handler ───────────────────────────────────────────────────────────────

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });

  // 1. Verify Telegram secret token
  const secretHeader = req.headers.get("X-Telegram-Bot-Api-Secret-Token");
  const webhookSecret = Deno.env.get("TELEGRAM_WEBHOOK_SECRET") ?? "";
  if (!verifySecretToken(secretHeader, webhookSecret)) {
    return new Response("unauthorized", { status: 401 });
  }

  // 2. Parse body
  let update: any;
  try { update = JSON.parse(await req.text()); }
  catch { return new Response("bad json", { status: 400 }); }

  const updateId: number | undefined = update?.update_id;
  if (typeof updateId !== "number") return new Response("no update_id", { status: 400 });

  const db = svc();

  // 3. Idempotency check — have we processed this update_id already?
  const { data: seen } = await db
    .from("telegram_processed_updates")
    .select("update_id")
    .eq("update_id", updateId)
    .maybeSingle();
  if (seen) return new Response("already processed", { status: 200 });

  // 3b. callback_query — a sprint adherence-tick button on the daily brief. Additive: the
  // message-ingestion path below is untouched. callback_data = tick:<sprintId>:<date>:<activity>.
  const cb = update.callback_query;
  if (cb) {
    const cbChatId: number | undefined = cb.message?.chat?.id;
    const cbMsgId: number | undefined = cb.message?.message_id;
    const data: string = cb.data ?? "";
    const ack = async () => { await db.from("telegram_processed_updates").insert({ update_id: updateId }); };

    // The tapping chat must be an ACTIVE identity; every DB op below is scoped to its
    // profile_id, so a tap can only ever read/write the tapper's own data.
    const { data: cbIdentity } = cbChatId === undefined ? { data: null } : await db
      .from("telegram_identities").select("profile_id, status").eq("chat_id", cbChatId).maybeSingle();
    if (!cbIdentity || cbIdentity.status !== "active" || cbChatId === undefined) {
      await answerCallback(cb.id, ""); await ack();
      return new Response("ok", { status: 200 });
    }
    const pid: string = cbIdentity.profile_id;
    const refresh = async (markup: unknown) => {
      if (cbMsgId !== undefined) await editKeyboard(cbChatId, cbMsgId, markup);
    };

    let m: RegExpExecArray | null;
    if ((m = /^menu:(\d{4}-\d{2}-\d{2})$/.exec(data))) {
      // 📝 Update today → expand to the top menu (training toggles + supplement slots).
      const { sprint, slots } = await getMenuData(db, pid, m[1]);
      await answerCallback(cb.id, "");
      await refresh(topMenu(m[1], sprint, slots));
    } else if ((m = /^back:(\d{4}-\d{2}-\d{2})$/.exec(data))) {
      const { sprint, slots } = await getMenuData(db, pid, m[1]);
      await answerCallback(cb.id, "");
      await refresh(topMenu(m[1], sprint, slots));
    } else if ((m = /^close:(\d{4}-\d{2}-\d{2})$/.exec(data))) {
      await answerCallback(cb.id, "");
      await refresh({ inline_keyboard: [[{ text: "📝 Update today", callback_data: `menu:${m[1]}` }]] });
    } else if ((m = /^slot:(\d{4}-\d{2}-\d{2}):([a-z]+)$/.exec(data))) {
      const { slots } = await getMenuData(db, pid, m[1]);
      await answerCallback(cb.id, "");
      await refresh(slotMenu(m[1], m[2], slots[m[2]] ?? []));
    } else if ((m = /^supp:(\d{4}-\d{2}-\d{2}):([0-9a-f-]{36})$/.exec(data))) {
      // Toggle the supplement, then re-render its slot drill-in with fresh state.
      const dateIso = m[1], suppId = m[2];
      const nowTaken = await toggleSupplement(db, pid, suppId, dateIso);
      const { slots } = await getMenuData(db, pid, dateIso);
      // find which slot this supplement is in (for the re-render)
      const slot = SLOT_ORDER.find((s) => (slots[s] ?? []).some((i) => i.id === suppId)) ?? "anytime";
      await answerCallback(cb.id, nowTaken ? "✅ logged" : "↩ unlogged");
      await refresh(slotMenu(dateIso, slot, slots[slot] ?? []));
    } else if ((m = /^tick:([^:]+):(\d{4}-\d{2}-\d{2}):([a-z]+)$/.exec(data))) {
      // Training adherence tick (inside the menu) → re-render the FULL top menu.
      const [, sprintId, dateIso, activity] = m;
      const day = await applyTick(db, sprintId, dateIso, activity, pid);
      await answerCallback(cb.id, day ? `✅ ${activity}` : "Couldn't log that");
      if (day) {
        const { sprint, slots } = await getMenuData(db, pid, dateIso);
        await refresh(topMenu(dateIso, sprint, slots));
      }
    } else {
      await answerCallback(cb.id, "");
    }
    await ack();
    return new Response("ok", { status: 200 });
  }

  // 4. Extract message (message or edited_message; ignore non-message updates)
  const msg = update.message ?? update.edited_message;
  if (!msg) {
    // Non-message update (inline query, etc.) — ack without enqueuing
    await db.from("telegram_processed_updates").insert({ update_id: updateId });
    return new Response("ok", { status: 200 });
  }

  const chatId: number = msg.chat?.id;
  const messageText: string | undefined = msg.text;

  // 5. Identity lookup
  const { data: identity } = await db
    .from("telegram_identities")
    .select("profile_id, status, is_minor")
    .eq("chat_id", chatId)
    .maybeSingle();

  // 6. Unknown or pending chat — try link-code activation, else reject
  if (!identity || identity.status === "pending") {
    const raw = messageText?.trim() ?? "";
    // Accept "/start <code>" or bare code
    const code = raw.startsWith("/start") ? raw.split(/\s+/)[1] : raw;

    if (code) {
      const { data: linkCode } = await db
        .from("telegram_link_codes")
        .select("profile_id, expires_at, used_at")
        .eq("code", code)
        .maybeSingle();

      if (linkCode && !linkCode.used_at && new Date(linkCode.expires_at) > new Date()) {
        // Valid code — resolve display name + is_minor
        const from = msg.from ?? {};
        const displayName = [from.first_name, from.last_name].filter(Boolean).join(" ") || "Unknown";

        const { data: profile } = await db
          .from("profiles")
          .select("relationship")
          .eq("id", linkCode.profile_id)
          .single();
        const isMinor = profile?.relationship === "child";

        if (identity?.status === "pending") {
          await db.from("telegram_identities").update({
            status: "active", display_name: displayName,
            is_minor: isMinor, linked_at: new Date().toISOString(), link_code: code,
          }).eq("chat_id", chatId);
        } else {
          await db.from("telegram_identities").insert({
            chat_id: chatId, profile_id: linkCode.profile_id,
            display_name: displayName, is_minor: isMinor,
            status: "active", link_code: code,
          });
        }

        await db.from("telegram_link_codes")
          .update({ used_at: new Date().toISOString() })
          .eq("code", code);

        await telegramSend(chatId, "✅ Account linked. Send me a health photo or message to log data.");
        await db.from("telegram_processed_updates").insert({ update_id: updateId });
        return new Response("ok", { status: 200 });
      }
    }

    // No valid code — send onboarding prompt, alert maintainer
    if (raw === "/start" || raw === "") {
      await telegramSend(chatId, "👋 Hi! Ask PC for a link code and send it here to connect your account.");
    } else {
      await telegramSend(chatId, "⚠️ Unknown account. Ask PC for a link code.");
    }
    await alertMaintainer(db, chatId, messageText);
    await db.from("telegram_processed_updates").insert({ update_id: updateId });
    return new Response("ok", { status: 200 });
  }

  // 7. Revoked
  if (identity.status === "revoked") {
    await telegramSend(chatId, "Your account access has been revoked. Contact PC.");
    await db.from("telegram_processed_updates").insert({ update_id: updateId });
    return new Response("ok", { status: 200 });
  }

  // 8. Active identity — route by message type
  const profileId: string = identity.profile_id;
  const photo: any[] | undefined = msg.photo;
  const document: any | undefined = msg.document;
  const isMediaMessage = !!(photo?.length || document);

  // Text-only messages → enqueue for the drain. The drain's LLM is the router: it
  // decides whether this is a LOG (food/supplement/biomarker, possibly several) or a
  // BRIEF request, and acts accordingly (see inbox_drain.py "unknown" prompt). No
  // regex routing here — the model decides. media_inbox FIRST, then update_id
  // (idempotency ordering, mirrors the media path). The pg_net trigger
  // (fn_media_inbox_notify) auto-fires the drain.
  if (!isMediaMessage) {
    const body = (messageText ?? "").trim();
    if (!body) {
      // Non-text payload with no caption (sticker, location, etc.) — nothing to do.
      await db.from("telegram_processed_updates").insert({ update_id: updateId });
      return new Response("ok", { status: 200 });
    }

    // /whoop (or "reconnect whoop") → reply with a one-tap WHOOP reconnect button. On-demand,
    // so it bypasses the dead-token-alert debounce (force=true). Intercepted before the
    // enqueue path so it never reaches the food/supplement drain.
    if (/^\/whoop\b/i.test(body) || /^reconnect\s+whoop\b/i.test(body)) {
      const sent = await sendReconnectPrompt(
        db, profileId, "Tap to reconnect your WHOOP — opens a quick WHOOP login, then you're done.", true,
      );
      if (!sent) await telegramSend(chatId, "Couldn't start the WHOOP reconnect — try again shortly.");
      await db.from("telegram_processed_updates").insert({ update_id: updateId });
      return new Response("ok", { status: 200 });
    }

    // Reply-to-clarify / reply-to-correct: a REPLY to a bot message that the drain tagged
    // with clarify_message_id. Two cases, both re-queue the original (combined caption +
    // image) so the drain re-extracts — we INSERT a fresh row (not UPDATE) so the AFTER-INSERT
    // pg_net trigger fires, then retire the original:
    //   • status='staged'  → ordinary clarify loop (item was never written).
    //   • status='done' + logged_food_ids → SUPERSEDE: the item was auto-LOGGED; delete those
    //     food_logs first so the re-extracted correction REPLACES it (no double-count).
    const replyToId: number | undefined = msg.reply_to_message?.message_id;
    if (replyToId) {
      const { data: target } = await db
        .from("media_inbox")
        .select("id, caption, storage_path, clarify_count, status, logged_food_ids, staged_review_ids")
        .eq("clarify_message_id", replyToId)
        .eq("profile_id", profileId)
        .in("status", ["staged", "done"])
        .limit(1)
        .maybeSingle();
      if (target) {
        await db.from("telegram_processed_updates").insert({ update_id: updateId });
        const prior = target.clarify_count ?? 0;
        if (prior >= 2) {
          // Cap clarification rounds — hand off to the maintainer rather than loop.
          await telegramSend(chatId, "Thanks — I'll have PC take a look at this one.");
          return new Response("ok", { status: 200 });
        }
        const isSupersede = target.status === "done"
          && Array.isArray(target.logged_food_ids) && target.logged_food_ids.length > 0;
        if (isSupersede) {
          // Delete the original logged entry/entries before re-queuing the correction.
          // service_role bypasses RLS; scope to this profile for safety.
          await db.from("food_logs").delete()
            .in("id", target.logged_food_ids).eq("profile_id", profileId);
        }
        // Staged item being clarified → retire its review row(s) so they don't linger as
        // phantoms in the maintainer review queue (the clarified re-extraction replaces them).
        if (target.status === "staged"
            && Array.isArray(target.staged_review_ids) && target.staged_review_ids.length > 0) {
          await db.from("stg_food_log_review")
            .update({ status: "merged", reviewed_at: new Date().toISOString() })
            .in("id", target.staged_review_ids).eq("profile_id", profileId);
        }
        await db.from("media_inbox").insert({
          profile_id: profileId,
          chat_id: chatId,
          kind: "unknown",
          storage_path: target.storage_path,          // re-analyse the original image if any
          caption: `${target.caption ?? ""}\n[clarification: ${body}]`.trim(),
          status: "pending",
          clarify_count: prior + 1,
        });
        await db
          .from("media_inbox")
          .update({ status: "done", clarify_message_id: null, stage_reason: "superseded by clarification" })
          .eq("id", target.id);
        await telegramSend(chatId, isSupersede ? "📥 Updating that — replacing the previous entry…"
                                               : "📥 Got it — updating that…");
        return new Response("ok", { status: 200 });
      }
    }

    const { error: txtErr } = await db.from("media_inbox").insert({
      profile_id: profileId,
      chat_id: chatId,
      kind: "unknown",
      storage_path: null,
      caption: body,
      status: "pending",
    });
    await db.from("telegram_processed_updates").insert({ update_id: updateId });
    await telegramSend(
      chatId,
      txtErr ? "⚠️ Couldn't process that — please try again." : "📥 On it…",
    );
    return new Response("ok", { status: 200 });
  }

  // Caption is DATA only — read for kind classification, never executed
  const caption: string | undefined = msg.caption ?? messageText;
  const kind = guessKind(caption);

  // media_group_id links photos from the same album burst (Phase 3A)
  const mediaGroupId: string | null = msg.media_group_id ?? null;

  // Download + store media
  let storagePath: string | null = null;
  const fileId: string = photo?.length
    ? photo[photo.length - 1].file_id   // take highest-res photo
    : document.file_id;
  const mimeType: string | undefined = document?.mime_type;
  storagePath = await storeMedia(db, fileId, profileId, updateId, mimeType);
  // download failure is non-fatal — enqueue with null path, Routine re-fetches

  // 9. INSERT media_inbox FIRST (before recording update_id — idempotency ordering)
  const { error: inboxErr } = await db.from("media_inbox").insert({
    profile_id: profileId,
    chat_id: chatId,
    kind,
    storage_path: storagePath,
    caption: caption ?? null,
    media_group_id: mediaGroupId,
    status: "pending",
  });

  if (inboxErr) {
    // Do NOT record update_id — Telegram retry will re-run this path
    return new Response(`enqueue failed: ${inboxErr.message}`, { status: 500 });
  }

  // 10. Record update_id only after successful enqueue
  await db.from("telegram_processed_updates").insert({ update_id: updateId });

  // 11. Ack sender
  const isMinor: boolean = identity.is_minor ?? false;
  const kindLabel = kind !== "unknown" ? kind : "health";
  const ack = isMinor
    ? `📥 Got your ${kindLabel} photo — I'll look at it shortly.`
    : `📥 Got it (${kindLabel}) — queued for processing.`;
  await telegramSend(chatId, ack);

  // 12. Fire Routine drain (Phase 3A) — deduped, non-blocking.
  // waitUntil keeps the isolate alive after the 200 is sent.
  const fireTask = maybeFireRoutine(db).catch(() => {});
  if (typeof EdgeRuntime !== "undefined") {
    EdgeRuntime.waitUntil(fireTask);
  } else {
    await fireTask;
  }

  return new Response("ok", { status: 200 });
});
