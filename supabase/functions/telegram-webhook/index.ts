// telegram-webhook — Phase 1 ingestion: auth, dedup, media→Storage, enqueue.
// Does NOT call the Routine (Phase 3 wires the trigger). Just enqueue + ack 200.
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

export function guessKind(caption: string | undefined): "food" | "workout" | "lab" | "dexa" | "unknown" {
  if (!caption) return "unknown";
  const t = caption.toLowerCase();
  if (/\b(food|meal|eat|ate|breakfast|lunch|dinner|snack|calories|protein|macro|carb|fat|glucose|creatine)\b/.test(t)) return "food";
  if (/\b(workout|exercise|gym|run|swim|bike|cycle|lift|strain|training|sport|hiit|cardio)\b/.test(t)) return "workout";
  if (/\b(lab|blood|test|result|report|hba1c|cholesterol|creatinine|tsh|vitamin|panel|rbc|wbc)\b/.test(t)) return "lab";
  if (/\b(dexa|dxa|body.?comp|composition|fat.?mass|lean.?mass|bone.?density)\b/.test(t)) return "dexa";
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

  // 8. Active identity — enqueue
  const profileId: string = identity.profile_id;
  // Caption is DATA only — read for kind classification, never executed
  const caption: string | undefined = msg.caption ?? messageText;
  const kind = guessKind(caption);

  // Photo or document → download + store; text-only → storage_path stays null
  const photo: any[] | undefined = msg.photo;
  const document: any | undefined = msg.document;
  let storagePath: string | null = null;

  if (photo?.length || document) {
    const fileId: string = photo?.length
      ? photo[photo.length - 1].file_id   // take highest-res photo
      : document.file_id;
    const mimeType: string | undefined = document?.mime_type;
    storagePath = await storeMedia(db, fileId, profileId, updateId, mimeType);
    // download failure is non-fatal — enqueue with null path, Routine re-fetches
  }

  // 9. INSERT media_inbox FIRST (before recording update_id — idempotency ordering)
  const { error: inboxErr } = await db.from("media_inbox").insert({
    profile_id: profileId,
    chat_id: chatId,
    kind,
    storage_path: storagePath,
    caption: caption ?? null,
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
  let ack: string;
  if (photo?.length || document) {
    const kindLabel = kind !== "unknown" ? kind : "health";
    // Minor-safe framing: growth/performance, never deficit language
    ack = isMinor
      ? `📥 Got your ${kindLabel} photo — I'll look at it shortly.`
      : `📥 Got it (${kindLabel}) — queued for processing.`;
  } else {
    const preview = (caption ?? "").slice(0, 80);
    ack = `📝 Noted: "${preview}"`;
  }
  await telegramSend(chatId, ack);

  return new Response("ok", { status: 200 });
});
