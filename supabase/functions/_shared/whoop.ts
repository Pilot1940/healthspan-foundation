// _shared/whoop.ts — WHOOP OAuth + token store + lazy refresh, used by both Edge functions.
// Auth: authorization_code with `offline` scope (yields refresh_token), client_secret_post,
// User-Agent header (WHOOP sits behind Cloudflare which 1010-blocks header-less clients).
import { createClient, SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2";

export const WHOOP_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth";
export const WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token";
export const WHOOP_API = "https://api.prod.whoop.com/developer";
export const SCOPES = "offline read:recovery read:cycles read:workout read:sleep read:profile read:body_measurement";
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36";

export function svc(): SupabaseClient {
  return createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,        // service_role: bypasses RLS for whoop_tokens
    { auth: { persistSession: false } },
  );
}

function form(o: Record<string, string>): string {
  return Object.entries(o).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
}

async function tokenPost(body: Record<string, string>): Promise<any> {
  const r = await fetch(WHOOP_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA, "Accept": "application/json" },
    body: form(body),
  });
  const txt = await r.text();
  if (!r.ok) throw new Error(`WHOOP token ${r.status}: ${txt.slice(0, 200)}`);
  return JSON.parse(txt);
}

export async function exchangeCode(code: string, redirectUri: string) {
  return await tokenPost({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri,
    client_id: Deno.env.get("WHOOP_CLIENT_ID")!,
    client_secret: Deno.env.get("WHOOP_CLIENT_SECRET")!,
  });
}

export async function refreshTokens(refresh_token: string) {
  return await tokenPost({
    grant_type: "refresh_token",
    refresh_token,
    scope: SCOPES,   // pass full scopes on refresh to prevent silent scope loss
    client_id: Deno.env.get("WHOOP_CLIENT_ID")!,
    client_secret: Deno.env.get("WHOOP_CLIENT_SECRET")!,
  });
}

export async function storeTokens(
  db: SupabaseClient, profileId: string, t: any, whoopUserId?: string | null,
) {
  const expires_at = new Date(Date.now() + (t.expires_in ?? 3600) * 1000).toISOString();
  const row: Record<string, unknown> = {
    profile_id: profileId,
    access_token: t.access_token,
    expires_at,
    scope: t.scope ?? SCOPES,
    updated_at: new Date().toISOString(),
  };
  if (t.refresh_token) row.refresh_token = t.refresh_token;   // persist ROTATED refresh token if returned
  if (whoopUserId) row.whoop_user_id = String(whoopUserId);
  const { error } = await db.from("whoop_tokens").upsert(row, { onConflict: "profile_id" });
  if (error) throw new Error(`store whoop_tokens: ${error.message}`);
}

// Lazy refresh-on-use: refresh only when expired/near-expiry. NO heartbeat.
export async function getValidAccessToken(db: SupabaseClient, profileId: string): Promise<string> {
  const { data, error } = await db.from("whoop_tokens").select("*").eq("profile_id", profileId).single();
  if (error || !data) throw new Error(`no whoop_tokens for profile ${profileId} — re-auth needed`);
  const skewMs = 120_000; // refresh if within 2 min of expiry
  if (data.access_token && data.expires_at && new Date(data.expires_at).getTime() - Date.now() > skewMs) {
    return data.access_token;
  }
  if (!data.refresh_token) throw new Error(`refresh token dead for profile ${profileId} — re-auth needed`);
  try {
    const fresh = await refreshTokens(data.refresh_token);
    await storeTokens(db, profileId, fresh, data.whoop_user_id);
    return fresh.access_token;
  } catch (e) {
    // BACKLOG #20: WHOOP rotates the refresh token on EVERY use; this webhook and the
    // CI-side refresh_recent can race on the same whoop_tokens row. The loser holds the
    // rotated-out token → "WHOOP token 400: invalid_request". Recovery: re-read the row —
    // if the winner already stored a fresh pair, use its access token (or retry ONCE
    // with the rotated-in refresh token). Anything else rethrows the original error.
    const { data: d2 } = await db.from("whoop_tokens").select("*").eq("profile_id", profileId).single();
    if (d2?.access_token && d2.expires_at && new Date(d2.expires_at).getTime() - Date.now() > skewMs) {
      return d2.access_token;
    }
    if (d2?.refresh_token && d2.refresh_token !== data.refresh_token) {
      const fresh2 = await refreshTokens(d2.refresh_token);
      await storeTokens(db, profileId, fresh2, d2.whoop_user_id);
      return fresh2.access_token;
    }
    throw e;
  }
}

export async function whoopGet(accessToken: string, path: string): Promise<any> {
  const r = await fetch(`${WHOOP_API}${path}`, {
    headers: { Authorization: `Bearer ${accessToken}`, "User-Agent": UA, Accept: "application/json" },
  });
  const txt = await r.text();
  if (!r.ok) throw new Error(`WHOOP GET ${path} ${r.status}: ${txt.slice(0, 200)}`);
  return JSON.parse(txt);
}

// ── Telegram re-auth: one-time tickets (whoop_oauth_codes, mig 065) ──────────────
// The ticket — not the raw profile_id — travels in the reconnect URL, so a shared
// link can't attach a stranger's WHOOP to a profile.

const SELF_BASE = () => Deno.env.get("SUPABASE_URL")!;

async function cfgNum(db: SupabaseClient, key: string, fallback: number): Promise<number> {
  const { data } = await db.from("system_config").select("value").eq("key", key).eq("is_active", true).maybeSingle();
  const n = Number(data?.value);
  return Number.isFinite(n) ? n : fallback;
}

/** Mint a single-use, expiring reconnect ticket for a profile. Returns the ticket id. */
export async function mintOAuthTicket(db: SupabaseClient, profileId: string): Promise<string> {
  const ttlMin = await cfgNum(db, "whoop.oauth_ticket_ttl_min", 30);
  const expires = new Date(Date.now() + ttlMin * 60_000).toISOString();
  const { data, error } = await db
    .from("whoop_oauth_codes")
    .insert({ profile_id: profileId, expires_at: expires })
    .select("code")
    .single();
  if (error || !data) throw new Error(`mintOAuthTicket: ${error?.message ?? "no row"}`);
  return data.code as string;
}

/** Resolve a ticket → profile_id WITHOUT consuming it (entry step). null if invalid/expired/used. */
export async function peekTicket(db: SupabaseClient, code: string): Promise<string | null> {
  const { data } = await db
    .from("whoop_oauth_codes")
    .select("profile_id, used_at, expires_at")
    .eq("code", code)
    .maybeSingle();
  if (!data || data.used_at || new Date(data.expires_at) < new Date()) return null;
  return data.profile_id as string;
}

/** Consume a ticket ONCE (callback step): marks used, returns profile_id, or null if already spent/expired. */
export async function consumeTicket(db: SupabaseClient, code: string): Promise<string | null> {
  // Atomic single-use: only flip rows still unused + unexpired.
  const { data } = await db
    .from("whoop_oauth_codes")
    .update({ used_at: new Date().toISOString() })
    .eq("code", code)
    .is("used_at", null)
    .gt("expires_at", new Date().toISOString())
    .select("profile_id");
  return data && data.length ? (data[0].profile_id as string) : null;
}

/** Has this profile been alerted (ticket minted) within the debounce window? */
export async function reauthAlertedRecently(db: SupabaseClient, profileId: string): Promise<boolean> {
  const hours = await cfgNum(db, "whoop.reauth_alert_debounce_hours", 24);
  const cutoff = new Date(Date.now() - hours * 3_600_000).toISOString();
  const { data } = await db
    .from("whoop_oauth_codes")
    .select("code")
    .eq("profile_id", profileId)
    .gt("created_at", cutoff)
    .limit(1);
  return !!(data && data.length);
}

/** Inline keyboard with a "Reconnect WHOOP" URL button → the whoop-oauth function. */
export function reconnectButton(ticket: string) {
  return {
    inline_keyboard: [[{
      text: "🔗 Reconnect WHOOP",
      url: `${SELF_BASE()}/functions/v1/whoop-oauth?t=${ticket}`,
    }]],
  };
}

/** Look up a profile's ACTIVE Telegram chat id (null if none). */
export async function activeChatId(db: SupabaseClient, profileId: string): Promise<number | null> {
  const { data } = await db
    .from("telegram_identities")
    .select("chat_id")
    .eq("profile_id", profileId)
    .eq("status", "active")
    .maybeSingle();
  return data?.chat_id ?? null;
}

/** Send a Telegram message (optionally with an inline keyboard). Best-effort. */
export async function tgSend(chatId: number, text: string, replyMarkup?: unknown): Promise<void> {
  const token = Deno.env.get("TELEGRAM_BOT_TOKEN")!;
  const body: Record<string, unknown> = { chat_id: chatId, text };
  if (replyMarkup) body.reply_markup = replyMarkup;
  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }).catch(() => {});
}

/** Mint a ticket + DM the profile's chat a Reconnect button. `force` bypasses the debounce.
 *  Returns true if a message was sent. */
export async function sendReconnectPrompt(
  db: SupabaseClient, profileId: string, lead: string, force = false,
): Promise<boolean> {
  if (!force && await reauthAlertedRecently(db, profileId)) return false;
  const chatId = await activeChatId(db, profileId);
  if (!chatId) return false;
  const ticket = await mintOAuthTicket(db, profileId);
  await tgSend(chatId, lead, reconnectButton(ticket));
  return true;
}
