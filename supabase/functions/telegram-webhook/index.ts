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

  // 4. Extract message (message or edited_message; ignore non-message updates)
  const msg = update.message ?? update.edited_message;
  if (!msg) {
    // Non-message update (inline query, callback, etc.) — ack without enqueuing
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

    // Reply-to-clarify: if this text is a REPLY to a staged item's review message, treat it
    // as a clarification — re-queue the original (combined caption + its image) so the drain
    // re-extracts with the new detail. We INSERT a fresh row (not UPDATE) so the AFTER-INSERT
    // pg_net trigger fires; the original staged row is retired.
    const replyToId: number | undefined = msg.reply_to_message?.message_id;
    if (replyToId) {
      const { data: staged } = await db
        .from("media_inbox")
        .select("id, caption, storage_path, clarify_count")
        .eq("clarify_message_id", replyToId)
        .eq("profile_id", profileId)
        .eq("status", "staged")
        .limit(1)
        .maybeSingle();
      if (staged) {
        await db.from("telegram_processed_updates").insert({ update_id: updateId });
        const prior = staged.clarify_count ?? 0;
        if (prior >= 2) {
          // Cap clarification rounds — hand off to the maintainer rather than loop.
          await telegramSend(chatId, "Thanks — I'll have PC take a look at this one.");
          return new Response("ok", { status: 200 });
        }
        await db.from("media_inbox").insert({
          profile_id: profileId,
          chat_id: chatId,
          kind: "unknown",
          storage_path: staged.storage_path,          // re-analyse the original image if any
          caption: `${staged.caption ?? ""}\n[clarification: ${body}]`.trim(),
          status: "pending",
          clarify_count: prior + 1,
        });
        await db
          .from("media_inbox")
          .update({ status: "done", clarify_message_id: null, stage_reason: "superseded by clarification" })
          .eq("id", staged.id);
        await telegramSend(chatId, "📥 Got it — updating that…");
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
