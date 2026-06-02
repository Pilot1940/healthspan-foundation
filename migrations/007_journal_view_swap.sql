-- =============================================================
-- 007_journal_view_swap.sql  (DESTRUCTIVE — drops whoop_journal TABLE)
-- Make whoop_journal_entries (003) canonical; replace the whoop_journal TABLE
-- with a security_invoker PIVOT VIEW that still exposes the 27 boolean columns,
-- so daily_health_summary, v_india_vs_travel, and the skill keep working.
--
-- GATES (all satisfied before applying):
--   * reconcile re-checked: entries reproduce the table 0-mismatch (verified live).
--   * fresh backup taken (day1-v3-complete).
--   * writer audit: only the (print-only) migrate script writes whoop_journal in-repo;
--     to protect any external processor we add an INSTEAD OF INSERT trigger that makes
--     the view WRITE-THROUGH (unpivots into whoop_journal_entries). So legacy
--     `INSERT INTO whoop_journal (...27 cols...)` keeps working transparently.
--   * dependents (daily_health_summary, v_india_vs_travel) dropped + recreated explicitly
--     in dependency order (no CASCADE), all security_invoker, all re-GRANTed.
-- =============================================================
BEGIN;

-- 1. Drop dependent views explicitly, in dependency order (recreated below).
--    Chain (dry-run confirmed): daily_health_summary <- supplement_exposure_daily
--    <- daily_supplement_outcomes; and whoop_journal <- daily_health_summary, v_india_vs_travel.
DROP VIEW IF EXISTS public.daily_supplement_outcomes;
DROP VIEW IF EXISTS public.supplement_exposure_daily;
DROP VIEW IF EXISTS public.v_india_vs_travel;
DROP VIEW IF EXISTS public.daily_health_summary;

-- 2. Drop the legacy table (its RLS policy, indexes, auto-sprint trigger go with it)
DROP TABLE IF EXISTS public.whoop_journal;

-- 3. Pivot VIEW — reproduces the 27 boolean columns from the long-format entries.
--    LEFT JOIN whoop_cycles for user_id + sprint_id (parity with old table columns).
CREATE VIEW public.whoop_journal
WITH (security_invoker = true) AS
SELECT
  e.profile_id,
  c.user_id,
  e.cycle_start,
  e.cycle_end,
  e.timezone,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='gluten_free_diet')        AS gluten_free_diet,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='consumed_meat')           AS consumed_meat,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='ate_during_daylight')     AS ate_during_daylight,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='consumed_added_sugar')    AS consumed_added_sugar,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='consumed_dairy')          AS consumed_dairy,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='consumed_protein')        AS consumed_protein,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='intermittent_fasting')    AS intermittent_fasting,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='consumed_fruits_veg')     AS consumed_fruits_veg,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='consumed_carbs')          AS consumed_carbs,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='consumed_caffeine')       AS consumed_caffeine,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='avoided_processed_foods') AS avoided_processed_foods,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='ate_close_to_bedtime')    AS ate_close_to_bedtime,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='took_fish_oil')           AS took_fish_oil,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='took_electrolytes')       AS took_electrolytes,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='took_calcium_supplement') AS took_calcium_supplement,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='hydrated_sufficiently')   AS hydrated_sufficiently,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='had_alcohol')             AS had_alcohol,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='read_non_screen_in_bed')  AS read_non_screen_in_bed,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='wore_mouth_tape')         AS wore_mouth_tape,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='hot_shower_before_bed')   AS hot_shower_before_bed,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='took_cold_shower')        AS took_cold_shower,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='saw_sunlight_on_waking')  AS saw_sunlight_on_waking,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='journaled_thoughts')      AS journaled_thoughts,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='learned_something')       AS learned_something,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='practiced_breathwork')    AS practiced_breathwork,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='connected_family_friends')AS connected_family_friends,
  bool_or(e.answered_yes) FILTER (WHERE b.slug='spent_time_outdoors')     AS spent_time_outdoors,
  c.sprint_id,
  max(e.source_file)                                                      AS source_file
FROM public.whoop_journal_entries e
JOIN public.journal_behaviors b ON b.id = e.behavior_id
LEFT JOIN public.whoop_cycles c ON c.profile_id = e.profile_id AND c.cycle_start = e.cycle_start
GROUP BY e.profile_id, c.user_id, e.cycle_start, e.cycle_end, e.timezone, c.sprint_id;

GRANT SELECT ON public.whoop_journal TO authenticated;

-- 4. WRITE-THROUGH: INSTEAD OF INSERT unpivots into whoop_journal_entries.
--    Any legacy `INSERT INTO whoop_journal (...27 cols...)` keeps working.
CREATE OR REPLACE FUNCTION public.whoop_journal_insert_through()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
DECLARE v_profile UUID;
BEGIN
  -- accept profile_id, else resolve from a supplied user_id (legacy writers)
  v_profile := COALESCE(NEW.profile_id,
                        (SELECT id FROM profiles WHERE auth_user_id = NEW.user_id));
  IF v_profile IS NULL THEN
    RAISE EXCEPTION 'whoop_journal insert: cannot resolve profile_id (profile_id and user_id both unmatched)';
  END IF;

  INSERT INTO whoop_journal_entries
    (profile_id, cycle_start, cycle_end, timezone, behavior_id, answered_yes, source_file)
  SELECT v_profile, NEW.cycle_start, NEW.cycle_end, NEW.timezone, b.id, x.val,
         COALESCE(NEW.source_file, 'whoop_journal_view_insert')
  FROM (VALUES
    ('gluten_free_diet', NEW.gluten_free_diet),
    ('consumed_meat', NEW.consumed_meat),
    ('ate_during_daylight', NEW.ate_during_daylight),
    ('consumed_added_sugar', NEW.consumed_added_sugar),
    ('consumed_dairy', NEW.consumed_dairy),
    ('consumed_protein', NEW.consumed_protein),
    ('intermittent_fasting', NEW.intermittent_fasting),
    ('consumed_fruits_veg', NEW.consumed_fruits_veg),
    ('consumed_carbs', NEW.consumed_carbs),
    ('consumed_caffeine', NEW.consumed_caffeine),
    ('avoided_processed_foods', NEW.avoided_processed_foods),
    ('ate_close_to_bedtime', NEW.ate_close_to_bedtime),
    ('took_fish_oil', NEW.took_fish_oil),
    ('took_electrolytes', NEW.took_electrolytes),
    ('took_calcium_supplement', NEW.took_calcium_supplement),
    ('hydrated_sufficiently', NEW.hydrated_sufficiently),
    ('had_alcohol', NEW.had_alcohol),
    ('read_non_screen_in_bed', NEW.read_non_screen_in_bed),
    ('wore_mouth_tape', NEW.wore_mouth_tape),
    ('hot_shower_before_bed', NEW.hot_shower_before_bed),
    ('took_cold_shower', NEW.took_cold_shower),
    ('saw_sunlight_on_waking', NEW.saw_sunlight_on_waking),
    ('journaled_thoughts', NEW.journaled_thoughts),
    ('learned_something', NEW.learned_something),
    ('practiced_breathwork', NEW.practiced_breathwork),
    ('connected_family_friends', NEW.connected_family_friends),
    ('spent_time_outdoors', NEW.spent_time_outdoors)
  ) AS x(slug, val)
  JOIN journal_behaviors b ON b.slug = x.slug
  ON CONFLICT (profile_id, cycle_start, behavior_id)
    DO UPDATE SET answered_yes = EXCLUDED.answered_yes;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_whoop_journal_insert_through ON public.whoop_journal;
CREATE TRIGGER trg_whoop_journal_insert_through
  INSTEAD OF INSERT ON public.whoop_journal
  FOR EACH ROW EXECUTE FUNCTION public.whoop_journal_insert_through();

-- 5. Recreate daily_health_summary (security_invoker) — references the new view by name
CREATE VIEW public.daily_health_summary
WITH (security_invoker = true) AS
SELECT
    c.profile_id, c.user_id, c.cycle_start::date AS date,
    c.recovery_score_pct, c.hrv_ms, c.resting_hr_bpm, c.blood_oxygen_pct,
    c.day_strain, c.sleep_performance_pct, c.asleep_duration_min,
    c.deep_sws_min, c.rem_min, c.sleep_efficiency_pct, c.sleep_debt_min,
    (SELECT COUNT(*)                 FROM whoop_workouts w WHERE w.cycle_start=c.cycle_start AND w.profile_id=c.profile_id) AS workout_count,
    (SELECT SUM(w.duration_min)      FROM whoop_workouts w WHERE w.cycle_start=c.cycle_start AND w.profile_id=c.profile_id) AS total_workout_min,
    (SELECT SUM(w.energy_burned_cal) FROM whoop_workouts w WHERE w.cycle_start=c.cycle_start AND w.profile_id=c.profile_id) AS total_workout_cal,
    j.had_alcohol, j.hydrated_sufficiently, j.consumed_added_sugar,
    (SELECT SUM(f.calories)  FROM food_logs f WHERE f.log_date=c.cycle_start::date AND f.profile_id=c.profile_id AND (f.is_day_summary IS NULL OR f.is_day_summary=false)) AS total_calories,
    (SELECT SUM(f.protein_g) FROM food_logs f WHERE f.log_date=c.cycle_start::date AND f.profile_id=c.profile_id AND (f.is_day_summary IS NULL OR f.is_day_summary=false)) AS total_protein_g,
    (SELECT SUM(f.carbs_g)   FROM food_logs f WHERE f.log_date=c.cycle_start::date AND f.profile_id=c.profile_id AND (f.is_day_summary IS NULL OR f.is_day_summary=false)) AS total_carbs_g,
    s.name AS sprint_name, s.slug AS sprint_slug
FROM whoop_cycles c
LEFT JOIN whoop_journal j ON j.profile_id=c.profile_id AND j.cycle_start=c.cycle_start
LEFT JOIN sprints s ON s.id=c.sprint_id
ORDER BY c.cycle_start DESC;
GRANT SELECT ON public.daily_health_summary TO authenticated;

-- 6. Recreate v_india_vs_travel (security_invoker) — references the new view by name
CREATE VIEW public.v_india_vs_travel
WITH (security_invoker = true) AS
SELECT
    COALESCE(t.destination, 'DELHI') AS location,
    COALESCE(t.is_altitude, false)   AS is_altitude,
    CASE WHEN t.destination IS NULL THEN 'India' ELSE 'Non-India' END AS region,
    COALESCE(j.had_alcohol, false)   AS had_alcohol,
    CASE WHEN t.destination IS NULL THEN 'India' ELSE 'Non-India' END
      || ' — ' || CASE WHEN COALESCE(j.had_alcohol,false) THEN 'Alcohol' ELSE 'Dry' END AS analysis_group,
    c.profile_id, c.cycle_start::date AS cycle_date,
    c.recovery_score_pct, c.resting_hr_bpm, c.hrv_ms,
    c.sleep_performance_pct, c.asleep_duration_min,
    c.deep_sws_min, c.rem_min, c.sleep_efficiency_pct,
    c.day_strain, c.respiratory_rate_rpm,
    COALESCE(j.consumed_caffeine, false)    AS consumed_caffeine,
    COALESCE(j.consumed_added_sugar, false) AS consumed_added_sugar,
    COALESCE(j.ate_close_to_bedtime, false) AS ate_close_to_bedtime,
    COALESCE(j.spent_time_outdoors, false)  AS spent_time_outdoors,
    t.alcohol_level AS trip_alcohol_level, t.activity_context
FROM whoop_cycles c
LEFT JOIN travel_log t ON c.cycle_start::date BETWEEN t.start_date AND t.end_date AND c.profile_id = t.profile_id
LEFT JOIN whoop_journal j ON c.cycle_start::date = j.cycle_start::date AND c.profile_id = j.profile_id
WHERE c.recovery_score_pct IS NOT NULL;
GRANT SELECT ON public.v_india_vs_travel TO authenticated;

-- 7. Recreate the 004 supplement correlation views (captured from live; D1 fix preserved)
CREATE VIEW public.supplement_exposure_daily
WITH (security_invoker = true) AS
 SELECT d.profile_id, d.date,
    e.effective_supplement_id AS supplement_id, e.regimen_id, e.via_compound,
    d.date - e.start_date AS days_since_start,
    CASE WHEN e.frequency = 'cyclical'::text AND e.cycle_days_on IS NOT NULL AND e.cycle_days_off IS NOT NULL
         THEN ((d.date - e.start_date) % (e.cycle_days_on + e.cycle_days_off)) < e.cycle_days_on
         ELSE true END AS on_phase,
    true AS is_active
   FROM daily_health_summary d
     JOIN resolved_supplement_exposure e
       ON e.profile_id = d.profile_id AND e.status <> 'planned'::text
      AND e.start_date IS NOT NULL AND d.date >= e.start_date
      AND d.date <= COALESCE(e.end_date, CURRENT_DATE);
GRANT SELECT ON public.supplement_exposure_daily TO authenticated;

CREATE VIEW public.daily_supplement_outcomes
WITH (security_invoker = true) AS
 SELECT d.profile_id, d.date, d.recovery_score_pct, d.hrv_ms, d.resting_hr_bpm,
    d.day_strain, d.sleep_performance_pct, d.asleep_duration_min, d.total_calories, d.sprint_name,
    COALESCE(ex.active_supplement_ids, ARRAY[]::uuid[]) AS active_supplement_ids,
    COALESCE(ex.active_supplement_names, ARRAY[]::text[]) AS active_supplement_names,
    COALESCE(ex.active_count, 0::bigint) AS active_supplement_count
   FROM daily_health_summary d
     LEFT JOIN ( SELECT sed.profile_id, sed.date,
            array_agg(DISTINCT sed.supplement_id) AS active_supplement_ids,
            array_agg(DISTINCT sup.name) AS active_supplement_names,
            count(DISTINCT sed.supplement_id) AS active_count
           FROM supplement_exposure_daily sed
             JOIN supplements sup ON sup.id = sed.supplement_id
          GROUP BY sed.profile_id, sed.date) ex ON ex.profile_id = d.profile_id AND ex.date = d.date;
GRANT SELECT ON public.daily_supplement_outcomes TO authenticated;

COMMIT;
