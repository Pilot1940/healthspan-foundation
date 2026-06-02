#!/usr/bin/env python3
"""
Backfill whoop_journal_entries (003) losslessly:
  1. Unpivot the existing whoop_journal (410 rows x 27 boolean cols) -> entries.
     Guarantees cycle_start alignment + exact reproduction of the booleans.
  2. Enrich `notes` (and quantity where numeric) from the raw journal CSVs,
     matched by (cycle local date, behaviour) — the 1,296 notes previously dropped.
Idempotent: upserts on (profile_id, cycle_start, behavior_id).
"""
import os, csv, glob, collections
from datetime import timezone
import psycopg2
from psycopg2.extras import execute_values
from dateutil import parser as dtp
from dotenv import load_dotenv

ROOT = "/Users/p.chitalkar/Library/CloudStorage/Dropbox/Development/HealthSpan/CODE/healthspan-foundation"
WHOOP = "/Users/p.chitalkar/Library/CloudStorage/Dropbox/Personal/Health - Fitness/Whoop Files"
load_dotenv(os.path.join(ROOT, ".env"))

# 27 whoop_journal boolean columns == behaviour slugs (1:1 by design)
SLUGS = ['gluten_free_diet','consumed_meat','ate_during_daylight','consumed_added_sugar',
 'consumed_dairy','consumed_protein','intermittent_fasting','consumed_fruits_veg',
 'consumed_carbs','consumed_caffeine','avoided_processed_foods','ate_close_to_bedtime',
 'took_fish_oil','took_electrolytes','took_calcium_supplement','hydrated_sufficiently',
 'had_alcohol','read_non_screen_in_bed','wore_mouth_tape','hot_shower_before_bed',
 'took_cold_shower','saw_sunlight_on_waking','journaled_thoughts','learned_something',
 'practiced_breathwork','connected_family_friends','spent_time_outdoors']

c = psycopg2.connect(os.environ["DATABASE_URL"]); cur = c.cursor()
cur.execute("SET statement_timeout = '120s'")

cur.execute("SELECT id FROM profiles WHERE relationship='self' LIMIT 1")
pc = cur.fetchone()[0]
cur.execute("SELECT slug, id FROM journal_behaviors")
slug2id = dict(cur.fetchall())
cur.execute("SELECT alias, behavior_id FROM journal_behavior_aliases")
alias2id = dict(cur.fetchall())

# ---- 1. unpivot whoop_journal -> entries ----
cols = ",".join(SLUGS)
cur.execute(f"SELECT profile_id, cycle_start, cycle_end, timezone, {cols} FROM whoop_journal")
rows = cur.fetchall()
payload = []
for r in rows:
    pid, cs, ce, tz = r[0], r[1], r[2], r[3]
    for slug, v in zip(SLUGS, r[4:]):
        payload.append((pid, cs, ce, tz, slug2id[slug], v))
execute_values(cur, """
    INSERT INTO whoop_journal_entries
      (profile_id, cycle_start, cycle_end, timezone, behavior_id, answered_yes, source_file)
    VALUES %s
    ON CONFLICT (profile_id, cycle_start, behavior_id)
    DO UPDATE SET answered_yes = EXCLUDED.answered_yes""",
    [(p, cs, ce, tz, bid, v, 'whoop_journal_unpivot') for (p, cs, ce, tz, bid, v) in payload],
    template="(%s,%s,%s,%s,%s,%s,%s)", page_size=1000)
c.commit()
print(f"unpivot: {len(rows)} journal rows -> {len(payload)} entries upserted")

# map (profile, local-date, behavior_id) -> cycle_start for note matching
cur.execute("SELECT cycle_start, timezone FROM whoop_journal WHERE profile_id=%s", (pc,))
date2cs = {}
for cs, tz in cur.fetchall():
    date2cs[cs.date()] = cs            # UTC date key (coarse; one cycle/day)

# ---- 2. enrich notes/quantity from CSVs ----
notes_set = 0; unmatched = 0
files = sorted(glob.glob(os.path.join(WHOOP, "**/journal_entries.csv"), recursive=True))
print(f"journal CSVs found: {len(files)}")
for fn in files:
    with open(fn, newline='') as f:
        for row in csv.DictReader(f):
            note = (row.get("Notes") or "").strip()
            if not note:
                continue
            q = (row.get("Question text") or "").strip()
            bid = alias2id.get(q) or alias2id.get(q.rstrip("?")) or slug2id.get(q)
            if not bid:
                continue
            try:
                d = dtp.parse(row["Cycle start time"]).astimezone(timezone.utc).date()
            except Exception:
                continue
            cs = date2cs.get(d)
            if not cs:
                unmatched += 1; continue
            cur.execute("""UPDATE whoop_journal_entries SET notes=%s
                           WHERE profile_id=%s AND cycle_start=%s AND behavior_id=%s
                             AND (notes IS NULL OR notes='')""",
                        (note, pc, cs, bid))
            notes_set += cur.rowcount
c.commit()
print(f"notes enrichment: {notes_set} entries got notes ({unmatched} csv rows unmatched by date)")

cur.execute("SELECT count(*), count(notes) FROM whoop_journal_entries")
tot, withnotes = cur.fetchone()
print(f"TOTAL entries: {tot}, with notes: {withnotes}")
cur.close(); c.close()
