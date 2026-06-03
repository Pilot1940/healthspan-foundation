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
    scope: "offline",
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
  const fresh = await refreshTokens(data.refresh_token);
  await storeTokens(db, profileId, fresh, data.whoop_user_id);
  return fresh.access_token;
}

export async function whoopGet(accessToken: string, path: string): Promise<any> {
  const r = await fetch(`${WHOOP_API}${path}`, {
    headers: { Authorization: `Bearer ${accessToken}`, "User-Agent": UA, Accept: "application/json" },
  });
  const txt = await r.text();
  if (!r.ok) throw new Error(`WHOOP GET ${path} ${r.status}: ${txt.slice(0, 200)}`);
  return JSON.parse(txt);
}
