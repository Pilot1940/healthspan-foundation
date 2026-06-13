"""lib/views.py — the single source of reporting truth (PROMPT 6).

A registry of named views: {name, description, sql, default_columns, chart_hint}.
Every emitter (report_md, dashboard, export_file) consumes THIS — no SQL is
duplicated anywhere else.

All SQL is profile-scoped via a `:profile_id` parameter and RLS-respecting (the
underlying tables enforce RLS under the caller's identity). Query rules baked in:
  * NULL recovery = NO SLEEP, never averaged as 0 (count scored vs no_sleep separately).
  * sprint status DERIVED inline from start/end dates (no status column exists).
  * canonical metric names: albumin_globulin_ratio / android_gynoid_ratio / ggt.
  * prefer the lab's PRINTED derived value (Rule #7) — views never recompute labs.
  * ad-hoc reads are read-only; the skill's role cannot write/DDL/delete.

run_view(conn, name, profile_id, **params) -> {"name","columns","rows","chart_hint","description"}.
"""
from __future__ import annotations

import psycopg2.extras


# ---------------------------------------------------------------------------
# The catalog. Each SQL uses %(profile_id)s and optional named params.
# ---------------------------------------------------------------------------

VIEWS: dict[str, dict] = {

    "v_recovery_30d": {
        "description": "Recovery, HRV, RHR over the last N days. NULL recovery = no sleep that "
                       "cycle (strap off / nap-only) — surfaced separately, never averaged as 0.",
        "params": {"days": 30},
        "chart_hint": "line",
        "default_columns": ["date", "recovery_score_pct", "hrv_ms", "resting_hr_bpm", "day_strain", "scored"],
        "sql": """
            SELECT cycle_start::date AS date,
                   recovery_score_pct, hrv_ms, resting_hr_bpm, day_strain,
                   sleep_performance_pct,
                   (recovery_score_pct IS NOT NULL) AS scored
            FROM whoop_cycles
            WHERE profile_id = %(profile_id)s
              AND cycle_start >= (CURRENT_DATE - (%(days)s || ' days')::interval)
            ORDER BY cycle_start
        """,
        # summary helper: averages computed ONLY over scored cycles
        "summary_sql": """
            SELECT count(*) FILTER (WHERE recovery_score_pct IS NOT NULL) AS scored_days,
                   count(*) FILTER (WHERE recovery_score_pct IS NULL)     AS no_sleep_days,
                   round(avg(recovery_score_pct) FILTER (WHERE recovery_score_pct IS NOT NULL), 1) AS avg_recovery,
                   round(avg(hrv_ms)              FILTER (WHERE recovery_score_pct IS NOT NULL), 1) AS avg_hrv,
                   round(avg(resting_hr_bpm)      FILTER (WHERE recovery_score_pct IS NOT NULL), 1) AS avg_rhr
            FROM whoop_cycles
            WHERE profile_id = %(profile_id)s
              AND cycle_start >= (CURRENT_DATE - (%(days)s || ' days')::interval)
        """,
    },

    "v_sleep_debt_30d": {
        "description": "Sleep need vs actual asleep + sleep debt, last N days (scored cycles only).",
        "params": {"days": 30},
        "chart_hint": "line",
        "default_columns": ["date", "asleep_duration_min", "sleep_need_min", "sleep_debt_min", "sleep_performance_pct"],
        "sql": """
            SELECT cycle_start::date AS date,
                   asleep_duration_min, sleep_need_min, sleep_debt_min,
                   sleep_performance_pct, sleep_efficiency_pct
            FROM whoop_cycles
            WHERE profile_id = %(profile_id)s
              AND sleep_performance_pct IS NOT NULL
              AND cycle_start >= (CURRENT_DATE - (%(days)s || ' days')::interval)
            ORDER BY cycle_start
        """,
    },

    "v_workout_zone_summary": {
        "description": "Time-in-zone per workout (last N days). Zone seconds → minutes; "
                       "high-intensity = Z4+Z5.",
        "params": {"days": 30},
        "chart_hint": "stacked_bar",
        "default_columns": ["date", "activity_name", "duration_min", "z2_min", "z3_min", "z4_min", "z5_min", "hi_intensity_min"],
        "sql": """
            SELECT workout_start::date AS date, activity_name,
                   round(duration_min, 1) AS duration_min,
                   round(COALESCE(hr_zone2_sec,0)/60.0, 1) AS z2_min,
                   round(COALESCE(hr_zone3_sec,0)/60.0, 1) AS z3_min,
                   round(COALESCE(hr_zone4_sec,0)/60.0, 1) AS z4_min,
                   round(COALESCE(hr_zone5_sec,0)/60.0, 1) AS z5_min,
                   round((COALESCE(hr_zone4_sec,0)+COALESCE(hr_zone5_sec,0))/60.0, 1) AS hi_intensity_min,
                   activity_strain
            FROM whoop_workouts
            WHERE profile_id = %(profile_id)s
              AND workout_start >= (CURRENT_DATE - (%(days)s || ' days')::interval)
            ORDER BY workout_start
        """,
    },

    "v_supplement_onoff": {
        "description": "Active supplement regimens with derived on/off status (cyclical aware) "
                       "and the supplement name.",
        "params": {},
        "chart_hint": "table",
        "default_columns": ["supplement", "dose", "timing", "frequency", "status", "start_date", "end_date"],
        "sql": """
            SELECT s.display_name AS supplement,
                   (r.dose_amount::text || COALESCE(' '||r.dose_unit,'')) AS dose,
                   array_to_string(r.timing, ', ') AS timing,
                   r.frequency,
                   r.status,
                   r.start_date, r.end_date,
                   r.purpose
            FROM supplement_regimens r
            JOIN supplements s ON s.id = r.supplement_id
            WHERE r.profile_id = %(profile_id)s
            ORDER BY (r.status='active') DESC, s.display_name
        """,
    },

    "v_india_vs_travel": {
        "description": "Recovery/sleep by location — India baseline vs overseas (existing DB view; "
                       "has analysis_group India-Dry / India-Alcohol / Non-India).",
        "params": {},
        "chart_hint": "bar",
        "default_columns": ["analysis_group", "n_days", "avg_recovery", "avg_rhr", "avg_hrv", "avg_sleep_perf"],
        "sql": """
            SELECT analysis_group,
                   count(*) AS n_days,
                   round(avg(recovery_score_pct), 1) AS avg_recovery,
                   round(avg(resting_hr_bpm), 1)     AS avg_rhr,
                   round(avg(hrv_ms), 1)             AS avg_hrv,
                   round(avg(sleep_performance_pct), 1) AS avg_sleep_perf
            FROM v_india_vs_travel
            WHERE profile_id = %(profile_id)s AND recovery_score_pct IS NOT NULL
            GROUP BY analysis_group
            ORDER BY n_days DESC
        """,
    },

    "v_biomarker_timeline": {
        "description": "A single biomarker over time vs its reference band; preserves below/above "
                       "detection qualifier. Pass marker=<metric_definitions.name>.",
        "params": {"marker": "hba1c"},
        "chart_hint": "line_band",
        "default_columns": ["measured_at", "value", "qualifier", "unit", "min_value", "max_value", "lab_name"],
        "sql": """
            SELECT b.measured_at::date AS measured_at,
                   b.value, b.qualifier, b.unit,
                   m.min_value, m.max_value, b.lab_name
            FROM biomarkers b
            JOIN metric_definitions m ON m.id = b.metric_definition_id
            WHERE b.profile_id = %(profile_id)s AND m.name = %(marker)s
              AND b.voided_at IS NULL
            ORDER BY b.measured_at
        """,
    },

    "abnormal_labs": {
        "description": "Latest value per biomarker that is out of its reference range "
                       "(value < min or > max). Shows the qualifier so <10 etc. is clear.",
        "params": {},
        "chart_hint": "table",
        "default_columns": ["marker", "value", "qualifier", "unit", "min_value", "max_value", "flag", "measured_at"],
        "sql": """
            WITH latest AS (
                SELECT DISTINCT ON (b.metric_definition_id)
                       b.metric_definition_id, b.value, b.qualifier, b.unit,
                       b.measured_at, m.name AS marker, m.display_name,
                       m.min_value, m.max_value
                FROM biomarkers b
                JOIN metric_definitions m ON m.id = b.metric_definition_id
                WHERE b.profile_id = %(profile_id)s
                  AND b.voided_at IS NULL
                ORDER BY b.metric_definition_id, b.measured_at DESC
            )
            SELECT COALESCE(display_name, marker) AS marker,
                   value, qualifier, unit, min_value, max_value,
                   CASE WHEN min_value IS NOT NULL AND value < min_value THEN 'LOW'
                        WHEN max_value IS NOT NULL AND value > max_value THEN 'HIGH' END AS flag,
                   measured_at::date AS measured_at
            FROM latest
            WHERE (min_value IS NOT NULL AND value < min_value)
               OR (max_value IS NOT NULL AND value > max_value)
            ORDER BY marker
        """,
    },

    "vo2_status": {
        "description": "VO2max by METHOD as separate series — measured/lab (vo2max_lab+relative, "
                       "ml/kg/min) is authoritative; estimated (WHOOP) is context. Never blended.",
        "params": {},
        # one line PER method (measured vs estimated) — never a single blended line
        "chart_hint": "multi_line",
        "default_columns": ["method", "series", "measured_at", "value", "unit"],
        "sql": """
            SELECT
              CASE WHEN m.name IN ('vo2max_lab','vo2max_relative') THEN 'measured'
                   WHEN m.name = 'vo2max_estimated' THEN 'estimated' END AS method,
              m.name AS series, b.measured_at::date AS measured_at, b.value, b.unit, b.source
            FROM biomarkers b JOIN metric_definitions m ON m.id = b.metric_definition_id
            WHERE b.profile_id = %(profile_id)s
              AND m.name IN ('vo2max_lab','vo2max_relative','vo2max_estimated')
              AND b.voided_at IS NULL
            ORDER BY method, series, measured_at
        """,
        "summary_sql": """
            SELECT round(sum(COALESCE(hr_zone4_sec,0)+COALESCE(hr_zone5_sec,0))/60.0, 1) AS hi_intensity_min_21d,
                   count(*) AS workouts_21d
            FROM whoop_workouts
            WHERE profile_id = %(profile_id)s
              AND workout_start >= (CURRENT_DATE - interval '21 days')
        """,
    },

    "hr_drop_compare": {
        "description": "Per-interval HR shape (peak, recovery valley, recovery drop) for workouts "
                       "that have digitised intervals — VO2 interval-quality tracking.",
        "params": {},
        "chart_hint": "scatter_line",
        "default_columns": ["date", "activity_name", "interval_index", "peak_hr_bpm", "recovery_hr_bpm", "recovery_drop_bpm", "time_to_peak_sec"],
        "sql": """
            SELECT w.workout_start::date AS date, w.activity_name,
                   iv.interval_index, iv.kind,
                   iv.peak_hr_bpm, iv.recovery_hr_bpm, iv.recovery_drop_bpm,
                   iv.time_to_peak_sec, iv.duration_sec
            FROM workout_intervals iv
            JOIN whoop_workouts w ON w.id = iv.workout_id
            WHERE iv.profile_id = %(profile_id)s
            ORDER BY w.workout_start, iv.interval_index
        """,
    },

    "sprints_status": {
        "description": "Training sprints with DERIVED status (no status column exists — computed "
                       "from start/end dates vs today).",
        "params": {},
        "chart_hint": "table",
        "default_columns": ["name", "location", "start_date", "end_date", "status", "goals"],
        "sql": """
            SELECT name, location, start_date, end_date,
                   -- goals is now an object {block_goals,weekly_plan,rules,adherence_log};
                   -- legacy rows are still a flat array. Return the goal-strings array either
                   -- way so existing consumers keep the original contract.
                   CASE WHEN jsonb_typeof(goals) = 'object' THEN goals->'block_goals'
                        ELSE goals END AS goals,
                   CASE WHEN start_date <= CURRENT_DATE AND end_date >= CURRENT_DATE THEN 'active'
                        WHEN end_date < CURRENT_DATE THEN 'completed'
                        ELSE 'upcoming' END AS status
            FROM sprints
            WHERE profile_id = %(profile_id)s
            ORDER BY start_date
        """,
    },
}


def list_views() -> list[dict]:
    """Return the catalog metadata (name + description + chart_hint + params)."""
    return [{"name": n, "description": v["description"],
             "chart_hint": v["chart_hint"], "params": v.get("params", {})}
            for n, v in VIEWS.items()]


def run_view(conn, name: str, profile_id: str, **params) -> dict:
    """Execute a named view; return {name, columns, rows, chart_hint, description, summary?}.

    Profile-scoped (RLS-respecting). Extra **params override the view's defaults.
    """
    if not hasattr(conn, "cursor"):
        raise RuntimeError(
            "run_view requires a direct_role (psycopg2) connection — the analytics catalog "
            "runs raw SQL that supabase_client/PostgREST mode cannot execute. Run from Claude "
            "Code/local (direct_role) for views; App mode supports the REST-backed brief + "
            "self-write paths instead."
        )
    spec = VIEWS.get(name)
    if spec is None:
        raise KeyError(f"unknown view {name!r}. Known: {', '.join(VIEWS)}")
    bind = dict(spec.get("params", {}))
    bind.update(params)
    bind["profile_id"] = profile_id

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(spec["sql"], bind)
    rows = [dict(r) for r in cur.fetchall()]
    columns = list(rows[0].keys()) if rows else spec.get("default_columns", [])

    out = {
        "name": name,
        "description": spec["description"],
        "chart_hint": spec["chart_hint"],
        "columns": columns,
        "rows": rows,
    }
    if "summary_sql" in spec:
        cur.execute(spec["summary_sql"], bind)
        srow = cur.fetchone()
        out["summary"] = dict(srow) if srow else {}

    # Observability (migration 025): log every catalog run for the maintainer. Catalog
    # views are pre-vetted, so no quality verdict / never flagged. Non-blocking — a
    # read-only connection or a non-maintainer's RLS just no-ops the audit (savepoint).
    from lib.sql_guard import log_query
    log_query(conn, profile_id, kind="catalog", name_or_sql=name,
              params={k: v for k, v in bind.items() if k != "profile_id"},
              row_count=len(rows))
    return out
