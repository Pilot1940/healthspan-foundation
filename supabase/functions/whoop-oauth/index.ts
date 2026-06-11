// whoop-oauth — WHOOP consent (offline scope → refresh token) + callback + token store.
// deploy: 2026-06-11 — Telegram reconnect flow: one-time ticket (?t=) state, store, TG confirm
// Deploy: supabase functions deploy whoop-oauth --no-verify-jwt
// Register redirect URI in WHOOP dashboard:
//   https://dsnydskkjwziynwmzfkh.supabase.co/functions/v1/whoop-oauth
//
// Entry (two ways to start):
//   GET /whoop-oauth?t=<ticket>        → Telegram reconnect: one-time ticket (mig 065) → consent
//   GET /whoop-oauth?profile_id=<uuid> → manual/admin                            → consent
// Callback:
//   GET /whoop-oauth?code=...&state=<t.ticket|p.profile> → exchange, store, Telegram confirm
import {
  WHOOP_AUTH_URL, SCOPES, exchangeCode, storeTokens, whoopGet, svc,
  peekTicket, consumeTicket, activeChatId, tgSend,
} from "../_shared/whoop.ts";

const SELF = `${Deno.env.get("SUPABASE_URL")!}/functions/v1/whoop-oauth`;

const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function html(body: string, status = 200): Response {
  return new Response(`<!doctype html><meta charset=utf-8><body style="font-family:system-ui;max-width:40rem;margin:3rem auto;padding:0 1rem">${body}</body>`,
    { status, headers: { "Content-Type": "text/html; charset=utf-8" } });
}

function consentRedirect(state: string): Response {
  const auth = `${WHOOP_AUTH_URL}?` + new URLSearchParams({
    response_type: "code",
    client_id: Deno.env.get("WHOOP_CLIENT_ID")!,
    redirect_uri: SELF,
    scope: SCOPES,
    state,
  }).toString();
  return Response.redirect(auth, 302);
}

Deno.serve(async (req) => {
  const url = new URL(req.url);
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const err = url.searchParams.get("error");

  if (err) return html(`<h2>WHOOP authorisation failed</h2><p>${esc(err)}: ${esc(url.searchParams.get("error_description") ?? "")}</p>`, 400);

  // --- Step 1: start consent ---
  if (!code) {
    const db = svc();
    const ticket = url.searchParams.get("t");
    if (ticket) {
      // Telegram reconnect: validate the one-time ticket (don't consume yet — that
      // happens on the callback so a single tap = a single store).
      const profileId = await peekTicket(db, ticket);
      if (!profileId) {
        return html(`<h2>Link expired</h2><p>This reconnect link is no longer valid. Send <code>/whoop</code> to the bot for a fresh one.</p>`, 400);
      }
      return consentRedirect(`t.${ticket}`);
    }
    const profileId = url.searchParams.get("profile_id");
    if (!profileId) {
      return html(`<h2>HealthSpan — Connect WHOOP</h2>
        <p>To reconnect, send <code>/whoop</code> to the Telegram bot and tap the button.</p>
        <p>(Admin: <code>${SELF}?profile_id=YOUR_PROFILE_UUID</code>.)</p>`);
    }
    return consentRedirect(`p.${profileId}`);
  }

  // --- Step 2: callback — resolve profile, exchange, store, confirm ---
  if (!state) return html("<h2>Missing state</h2>", 400);
  const db = svc();
  let profileId: string | null;
  if (state.startsWith("t.")) {
    profileId = await consumeTicket(db, state.slice(2));   // single-use
    if (!profileId) return html(`<h2>Link already used or expired</h2><p>Send <code>/whoop</code> to the bot for a fresh link.</p>`, 400);
  } else if (state.startsWith("p.")) {
    profileId = state.slice(2);
  } else {
    profileId = state;   // legacy raw-profile_id state
  }

  try {
    const tokens = await exchangeCode(code, SELF);
    let whoopUserId: string | null = null;
    try {
      const prof = await whoopGet(tokens.access_token, "/v2/user/profile/basic");
      whoopUserId = prof?.user_id != null ? String(prof.user_id) : null;
    } catch (_) { /* non-fatal; tokens still stored */ }
    await storeTokens(db, profileId, tokens, whoopUserId);

    // Telegram confirmation (best-effort) so the user knows it worked without checking the tab.
    try {
      const chatId = await activeChatId(db, profileId);
      if (chatId) await tgSend(chatId, "✅ WHOOP reconnected — your data will sync from now on.");
    } catch (_) { /* ignore */ }

    return html(`<h2>✅ WHOOP connected</h2>
      <p>You're all set${whoopUserId ? ` (WHOOP user ${esc(whoopUserId)})` : ""}. You can close this tab —
      syncing resumes automatically, and you'll only come back here if the connection ever drops.</p>`);
  } catch (e) {
    return html(`<h2>Token exchange failed</h2><pre>${esc((e as Error).message)}</pre>`, 500);
  }
});
