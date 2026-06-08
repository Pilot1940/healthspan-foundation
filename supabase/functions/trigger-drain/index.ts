// trigger-drain: fires GitHub repository_dispatch when media_inbox gets a new row.
// Deduplicates within a 15s window — albums arrive in <1s so this batches them
// without orphaning back-to-back meals.
//
// Called from fn_media_inbox_notify (pg_net trigger) — internal Supabase infrastructure.
// verify_jwt = false so no Supabase JWT is required.
//
// Dedup: atomic compare-and-set via UPDATE ... WHERE updated_at < cutoff.
//   Concurrent calls both hit the DB; only one UPDATE wins (affected rows = 1).
//   The loser sees 0 rows and returns skipped — no thundering herd.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const GITHUB_REPO = "Pilot1940/healthspan-foundation";
const DEDUP_KEY = "trigger_drain.last_dispatch_ts";
const DEDUP_WINDOW_SEC = 15;

Deno.serve(async (req) => {
  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const ghToken = Deno.env.get("GH_DISPATCH_TOKEN")!;

    const db = createClient(supabaseUrl, serviceKey, {
      auth: { persistSession: false },
    });

    // --- dedup: atomic compare-and-set (mirrors telegram-webhook maybeFireRoutine) ---
    // UPDATE wins only if updated_at < cutoff — concurrent calls both hit the DB
    // but only one UPDATE will match, preventing double-dispatch.
    const nowMs = Date.now();
    const cutoff = new Date(nowMs - DEDUP_WINDOW_SEC * 1000).toISOString();
    const nowIso = new Date(nowMs).toISOString();

    const { data: won } = await db
      .from("system_config")
      .update({ value: nowIso, updated_at: nowIso })
      .eq("key", DEDUP_KEY)
      .eq("is_active", true)
      .lt("updated_at", cutoff)
      .select("id");

    if (!won?.length) {
      return new Response(
        JSON.stringify({ skipped: true }),
        { headers: { "Content-Type": "application/json" } },
      );
    }

    // --- fire GitHub repository_dispatch ---
    const resp = await fetch(
      `https://api.github.com/repos/${GITHUB_REPO}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${ghToken}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ event_type: "inbox-drain" }),
      },
    );

    if (!resp.ok) {
      const body = await resp.text();
      return new Response(JSON.stringify({ error: body }), { status: 502 });
    }

    return new Response(
      JSON.stringify({ dispatched: true, ts: nowIso }),
      { headers: { "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ error: String(err) }),
      { status: 500 },
    );
  }
});
