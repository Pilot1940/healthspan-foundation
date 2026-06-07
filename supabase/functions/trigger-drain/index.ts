// trigger-drain: fires GitHub repository_dispatch when media_inbox gets a new row.
// Deduplicates within a 120s window so rapid album uploads only trigger one run.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const GITHUB_REPO = "Pilot1940/healthspan-foundation";
const DEDUP_KEY = "trigger_drain.last_dispatch_ts";
const DEDUP_WINDOW_SEC = 120;

Deno.serve(async (_req) => {
  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const ghToken = Deno.env.get("GH_DISPATCH_TOKEN")!;

    const db = createClient(supabaseUrl, serviceKey, {
      auth: { persistSession: false },
    });

    // --- dedup: skip if dispatched recently ---
    const { data: row } = await db
      .from("system_config")
      .select("value")
      .eq("key", DEDUP_KEY)
      .maybeSingle();

    const lastTs = row?.value ? Number(row.value) : 0;
    const nowSec = Math.floor(Date.now() / 1000);

    if (nowSec - lastTs < DEDUP_WINDOW_SEC) {
      return new Response(
        JSON.stringify({ skipped: true, age_sec: nowSec - lastTs }),
        { headers: { "Content-Type": "application/json" } },
      );
    }

    // Stamp before dispatching — prevents thundering herd if GH API is slow
    await db
      .from("system_config")
      .upsert({ key: DEDUP_KEY, value: String(nowSec) }, { onConflict: "key" });

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
      JSON.stringify({ dispatched: true, ts: nowSec }),
      { headers: { "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ error: String(err) }),
      { status: 500 },
    );
  }
});
