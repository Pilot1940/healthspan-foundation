"""Case 4 (counterpart) — PC side of the isolation proof (read-only).

Dea's run shows she sees 0 PC rows. That alone is consistent with two stories:
the rows are HIDDEN, or the rows simply don't exist. This run kills the second
story: scoped as PC (pc_skill.config.json), the very tables Dea saw as empty
(biomarkers, health_context_notes) return PC's rows — they exist, RLS hides them.
It also checks isolation is SYMMETRIC: PC must see 0 of Dea's rows.
"""
import lib.db as db

PC_CFG = "config/pc_skill.config.json"
DEA_PROFILE = "3eed5503-a26f-4b88-bb76-075208fa5de3"

cfg = db.load_config(PC_CFG)
PC_PROFILE = cfg["profile_id"]
conn, mode = db.get_app_connection(cfg)
cur = conn.cursor()

cur.execute("SELECT current_user, current_setting('request.jwt.claims', true)")
who = cur.fetchone()
print(f"== scoped as PC: mode={mode} role={who[0]} ==")
print(f"   jwt sub matches PC auth_user_id: "
      f"{cfg['connection']['direct_role']['auth_user_id'] in (who[1] or '')}")
print(f"   PC profile={PC_PROFILE}\n   Dea profile={DEA_PROFILE}\n")

TABLES = ("biomarkers", "whoop_cycles", "whoop_sleeps", "whoop_workouts",
          "food_logs", "supplement_intake_logs", "health_context_notes")

print("A. Rows PC can see — the ones Dea saw as 0 for biomarkers/notes prove existence")
for tbl in TABLES:
    cur.execute(f"SELECT count(*) FROM {tbl}")                       # RLS auto-scopes to PC
    pc_total = cur.fetchone()[0]
    cur.execute(f"SELECT count(*) FROM {tbl} WHERE profile_id = %s", (DEA_PROFILE,))
    dea_rows = cur.fetchone()[0]                                     # must be 0 — symmetric
    flag = "LEAK!!" if dea_rows else "ok"
    print(f"     {tbl:26} PC visible: {pc_total:>4}   Dea rows visible to PC: {dea_rows:>4}   [{flag}]")

print("\nB. distinct profile_ids PC can see across his WHOOP cycles (expect only his):")
cur.execute("SELECT DISTINCT profile_id FROM whoop_cycles")
ids = [str(r[0]) for r in cur.fetchall()]
print(f"     {ids}")
print(f"     only PC's id present: {ids == [PC_PROFILE] or ids == []}")

print("\nC. profiles table visibility (how many profiles each side can enumerate):")
cur.execute("SELECT count(*) FROM profiles")
print(f"     profiles visible to PC: {cur.fetchone()[0]}")

conn.rollback(); conn.close()
print("\n== done (read-only) ==")
