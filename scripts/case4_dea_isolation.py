"""Case 4 — Dea cross-profile isolation (read-only, scoped to Dea's config).

Runs Case-1-style reads under dea.config.json and proves RLS isolation:
  A. Dea's OWN recovery is visible (her WHOOP data).
  B. Dea has 0 biomarkers (none ingested for her).
  C. Dea canNOT see PC's rows — every cross-profile probe returns 0, and the
     only profile_id she can see anywhere is her own.
Same role (authenticated), same connection credential — ONLY the JWT claim
differs. has_profile_access() is the sole isolation boundary; this tests it.
"""
import lib.db as db
from lib.views import run_view

DEA_CFG = "config/dea.config.json"
PC_PROFILE = "21f69003-46f8-4e1c-a928-b1f694ce4aff"

cfg = db.load_config(DEA_CFG)
DEA_PROFILE = cfg["profile_id"]
conn, mode = db.get_app_connection(cfg)
cur = conn.cursor()

cur.execute("SELECT current_user, current_setting('request.jwt.claims', true)")
who = cur.fetchone()
print(f"== scoped as Dea: mode={mode} role={who[0]} ==")
print(f"   jwt claim sub matches Dea auth_user_id: {cfg['connection']['direct_role']['auth_user_id'] in (who[1] or '')}")
print(f"   PC profile={PC_PROFILE}\n   Dea profile={DEA_PROFILE}\n")

# -------------------------------------------------------------------------
print("A. Dea's OWN recovery (v_recovery_30d, Case-1 view) — her data is visible")
rec = run_view(conn, "v_recovery_30d", DEA_PROFILE)
for r in rec["rows"]:
    print("    ", {k: r[k] for k in rec["columns"]})
print()

# -------------------------------------------------------------------------
print("B. Dea's biomarker count (expect 0 — none ingested for her)")
cur.execute("SELECT count(*) FROM biomarkers")            # RLS auto-scopes
print(f"     biomarkers visible to Dea: {cur.fetchone()[0]}\n")

# -------------------------------------------------------------------------
print("C. Cross-profile probes — Dea must NOT see PC's rows")
for tbl in ("biomarkers", "whoop_cycles", "whoop_sleeps", "whoop_workouts",
            "food_logs", "supplement_intake_logs", "health_context_notes"):
    cur.execute(f"SELECT count(*) FROM {tbl} WHERE profile_id = %s", (PC_PROFILE,))
    pc_rows = cur.fetchone()[0]
    cur.execute(f"SELECT count(*) FROM {tbl}")
    total = cur.fetchone()[0]
    flag = "LEAK!!" if pc_rows else "ok"
    print(f"     {tbl:26} PC rows visible: {pc_rows:>4}   total visible: {total:>4}   [{flag}]")

print("\n   distinct profile_ids Dea can see across her WHOOP cycles (expect only hers):")
cur.execute("SELECT DISTINCT profile_id FROM whoop_cycles")
ids = [str(r[0]) for r in cur.fetchall()]
print(f"     {ids}")
print(f"     only Dea's id present: {ids == [DEA_PROFILE] or ids == []}")

# attempt to read a PC context note directly by his profile (must be empty)
cur.execute("SELECT count(*) FROM health_context_notes WHERE profile_id=%s", (PC_PROFILE,))
print(f"     PC's session-memory notes readable by Dea: {cur.fetchone()[0]} (expect 0)")

conn.rollback(); conn.close()
print("\n== done (read-only) ==")
