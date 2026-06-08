// whoop-oauth — one-time consent (offline scope → refresh token) + callback + token store.
// Deploy: supabase functions deploy whoop-oauth --no-verify-jwt
// Register redirect URI in WHOOP dashboard:
//   https://dsnydskkjwziynwmzfkh.supabase.co/functions/v1/whoop-oauth
//
// Flow:
//   GET /whoop-oauth?profile_id=<uuid>           → redirect to WHOOP consent (state=profile_id)
//   GET /whoop-oauth?code=...&state=<profile_id> → exchange, fetch whoop user id, store, success page
import { WHOOP_AUTH_URL, SCOPES, exchangeCode, storeTokens, whoopGet, svc } from "../_shared/whoop.ts";

const SELF = `${Deno.env.get("SUPABASE_URL")!.replace(".supabase.co", ".supabase.co")}/functions/v1/whoop-oauth`;

const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function html(body: string, status = 200): Response {
  return new Response(`<!doctype html><meta charset=utf-8><body style="font-family:system-ui;max-width:40rem;margin:3rem auto;padding:0 1rem">${body}</body>`,
    { status, headers: { "Content-Type": "text/html; charset=utf-8" } });
}

Deno.serve(async (req) => {
  const url = new URL(req.url);
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const err = url.searchParams.get("error");

  if (err) return html(`<h2>WHOOP authorisation failed</h2><p>${esc(err)}: ${esc(url.searchParams.get("error_description") ?? "")}</p>`, 400);

  // --- Step 1: start consent ---
  if (!code) {
    const profileId = url.searchParams.get("profile_id");
    if (!profileId) {
      return html(`<h2>HealthSpan — Connect WHOOP</h2>
        <p>Open this URL with your <code>profile_id</code> to start:</p>
        <pre>${SELF}?profile_id=YOUR_PROFILE_UUID</pre>`);
    }
    const auth = `${WHOOP_AUTH_URL}?` + new URLSearchParams({
      response_type: "code",
      client_id: Deno.env.get("WHOOP_CLIENT_ID")!,
      redirect_uri: SELF,
      scope: SCOPES,
      state: profileId,                      // carries which profile this consent is for
    }).toString();
    return Response.redirect(auth, 302);
  }

  // --- Step 2: callback — exchange + store ---
  if (!state) return html("<h2>Missing state (profile_id)</h2>", 400);
  try {
    const tokens = await exchangeCode(code, SELF);
    const db = svc();
    // fetch WHOOP user id so the webhook can map user_id → profile_id
    let whoopUserId: string | null = null;
    try {
      const prof = await whoopGet(tokens.access_token, "/v2/user/profile/basic");
      whoopUserId = prof?.user_id != null ? String(prof.user_id) : null;
    } catch (_) { /* non-fatal; tokens still stored */ }
    await storeTokens(db, state, tokens, whoopUserId);
    return html(`<h2>✅ WHOOP connected</h2>
      <p>Tokens stored for profile <code>${esc(state)}</code>${whoopUserId ? ` (WHOOP user ${esc(whoopUserId)})` : ""}.</p>
      <p>You can close this tab. The skill will refresh the token automatically; you only return here if the refresh token ever dies.</p>`);
  } catch (e) {
    return html(`<h2>Token exchange failed</h2><pre>${esc((e as Error).message)}</pre>`, 500);
  }
});
