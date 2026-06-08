"""lib/contract.py — the shared ingestion contract.

Every ingestion source obeys the same four steps
(HealthSpan-Ingestion-Export-Architecture.md §2):

    1. RESOLVE  — map raw payload → catalog FK + confidence + normalised row
    2. VALIDATE — types, FK existence, numeric range vs metric_definitions
    3. WRITE    — idempotent INSERT … ON CONFLICT (natural key) DO UPDATE
    4. LOG      — wearable_sync_log run row + per-record wearable_sync_errors

All DB-touching functions take an explicit psycopg2 ``conn`` so they are unit
testable against a mock.

Principles (mirroring the BaySys data-quality spec):
  * Never silently mutate — bad/uncertain data is flagged or staged, never coerced.
  * Idempotent — write() upserts on the natural key (migration 008a).
  * Atomic per record — ingest_record() wraps each row in a SAVEPOINT; one bad
    row fails itself (logged) and the run continues.
  * Low-confidence / unresolved / out-of-range → the matching stg_*_review
    table, NOT prod.
  * No hardcoded thresholds — the confidence cutoff comes from system_config
    (CLAUDE.md rule #1).
  * Trust the lab's PRINTED derived value — never re-derive it (prevention rule #7).
    Labs use model variants a naive formula won't reproduce: HOMA-2 (not the simple
    glucose*insulin/405 HOMA1), CKD-EPI 2021 (not MDRD), HOMA2 %S = 100/IR, etc.
    Store the printed number. Only compute a derived value when the report omits it,
    and then record the formula + inputs in notes and flag it derived. Unit
    conversions (parse_number) are arithmetic and fine; verify the source unit from
    the printed reference range, not a guess.
"""
from __future__ import annotations

import json
import logging
import re
import uuid as _uuid

# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------


class ContractError(RuntimeError):
    """A record failed validation in a way that is not auto-recoverable."""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


class ConfigError(RuntimeError):
    """A required system_config value is missing (rule #1: no hardcoded thresholds)."""


# ----------------------------------------------------------------------------
# Prevention rule #1 — number parsing. Strip thousands separators + units before
# storing, and preserve a below/above-detection qualifier. Root-cause fix for the
# audit's "1,020 kcal" → 20 corruption. Returns (value: float|None, qualifier: str|None).
# ----------------------------------------------------------------------------

_QUALIFIERS = ("<=", ">=", "<", ">")   # real detection bounds; '~' is approximate noise
_NUM_RE = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?|[-+]?\.\d+")


def parse_number(raw):
    """'1,020 kcal' -> (1020.0, None); '<10.00 mg/dL' -> (10.0, '<'); 5.2 -> (5.2, None).

    Strips thousands separators and trailing units; a leading '~' (approximately)
    is dropped as noise; a leading < > <= >= is captured as the qualifier.
    Returns (None, None) if no number is present.
    """
    if raw is None:
        return (None, None)
    if isinstance(raw, (int, float)):
        return (float(raw), None)
    s = str(raw).strip().lstrip("~").strip()
    qualifier = None
    for q in _QUALIFIERS:  # longest first (<= before <)
        if s.startswith(q):
            qualifier = q
            s = s[len(q):].strip()
            break
    m = _NUM_RE.search(s)
    if not m:
        return (None, qualifier)
    return (float(m.group(0).replace(",", "")), qualifier)


# ----------------------------------------------------------------------------
# Identifier safety — table/column names cannot be parameterised, so they are
# validated against a strict pattern and double-quoted. (They come from our own
# code, never user input, but defence in depth is cheap.)
# ----------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(name: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f'"{name}"'


def _as_jsonb_param(raw):
    """Encode a Python value for a jsonb column (returns a JSON string or None)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, default=str)


# ----------------------------------------------------------------------------
# Config (thresholds live in system_config — rule #1)
# ----------------------------------------------------------------------------


def get_config(conn, key: str):
    """Return a system_config value (jsonb → native Python via psycopg2)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT value FROM system_config WHERE key = %s AND is_active", (key,)
    )
    row = cur.fetchone()
    if row is None:
        raise ConfigError(
            f"system_config is missing key {key!r}. "
            f"Per CLAUDE.md rule #1 thresholds are not hardcoded — seed it first."
        )
    return row[0]


def confidence_min(conn) -> float:
    """The minimum confidence to write straight to prod (else → staging)."""
    return float(get_config(conn, "ingest.confidence_threshold"))


# ----------------------------------------------------------------------------
# Per-table metadata
# ----------------------------------------------------------------------------

# Columns whose presence/non-null is required before a row may reach prod.
_REQUIRED = {
    "biomarkers": ["profile_id", "metric_definition_id", "measured_at", "value"],
    "food_logs": ["profile_id", "log_date"],
    "supplement_intake_logs": ["profile_id", "supplement_id", "taken_at"],
    "whoop_cycles": ["profile_id", "cycle_start"],
    "whoop_sleeps": ["profile_id", "sleep_onset"],
    "whoop_workouts": ["profile_id", "workout_start"],
    "weight_logs": ["profile_id", "logged_at"],
}

# Foreign keys to verify exist (column → referenced table, all keyed on id).
_FK_MAP = {
    "biomarkers": {"profile_id": "profiles", "metric_definition_id": "metric_definitions"},
    "food_logs": {"profile_id": "profiles"},
    "supplement_intake_logs": {"profile_id": "profiles", "supplement_id": "supplements"},
    "whoop_cycles": {"profile_id": "profiles"},
    "whoop_sleeps": {"profile_id": "profiles"},
    "whoop_workouts": {"profile_id": "profiles"},
    "weight_logs": {"profile_id": "profiles"},
}

# prod table → (staging table, adapter mapping a source dict → a staging row).
# ``source`` is {**raw, **normalised} so adapters can read both the original
# extracted text and the resolved FK.


def _stg_biomarker(source, confidence, raw_text):
    return {
        "profile_id": source.get("profile_id"),
        "metric_definition_id": source.get("metric_definition_id"),
        "extracted_name": source.get("extracted_name")
        or source.get("metric")
        or source.get("name"),
        "extracted_value": source.get("value"),
        "extracted_unit": source.get("unit"),
        "measured_at": source.get("measured_at"),
        "confidence": confidence,
        "status": "pending",
        "raw_text": raw_text,
    }


def _stg_food(source, confidence, raw_text):
    return {
        "profile_id": source.get("profile_id"),
        "meal_type": source.get("meal_type"),
        "description": source.get("description"),
        "calories": source.get("calories"),
        "protein_g": source.get("protein_g"),
        "carbs_g": source.get("carbs_g"),
        "fat_g": source.get("fat_g"),
        "fiber_g": source.get("fiber_g"),
        "foods": _as_jsonb_param(source.get("foods")),
        "verdict": source.get("verdict"),
        "confidence": confidence,
        "status": "pending",
        "raw_text": raw_text,
    }


def _stg_supplement(source, confidence, raw_text):
    return {
        "profile_id": source.get("profile_id"),
        "supplement_id": source.get("supplement_id"),
        "extracted_name": source.get("extracted_name") or source.get("name"),
        "extracted_dose": source.get("dose_amount") or source.get("extracted_dose"),
        "extracted_unit": source.get("dose_unit") or source.get("extracted_unit"),
        "taken_at": source.get("taken_at"),
        "confidence": confidence,
        "status": "pending",
        "raw_text": raw_text,
    }


_STG_SPEC = {
    "biomarkers": ("stg_biomarker_review", _stg_biomarker),
    "food_logs": ("stg_food_log_review", _stg_food),
    "supplement_intake_logs": ("stg_supplement_intake_review", _stg_supplement),
}


# ----------------------------------------------------------------------------
# Step 1 — RESOLVE
# ----------------------------------------------------------------------------


def resolve(conn, kind: str, raw: dict) -> dict:
    """Map a raw payload to a catalog FK + confidence + normalised prod row.

    Returns ``{"fk": <uuid|None>, "confidence": <0..1>, "normalised": {...}}``.
    ``normalised`` contains only prod-table columns (safe to pass to write()).
    An exact catalog match scores 1.0; a substring match 0.5; no match 0.0
    (``fk=None``) → the caller routes it to staging. Never invents an FK.
    """
    cur = conn.cursor()
    raw = dict(raw or {})

    if kind == "biomarker":
        name = raw.get("metric") or raw.get("name") or raw.get("extracted_name")
        fk, conf, unit = None, 0.0, raw.get("unit")
        if name:
            cur.execute(
                """SELECT id, unit FROM metric_definitions
                   WHERE is_active AND lower(name) = lower(%s)
                   ORDER BY 1 LIMIT 1""",
                (name,),
            )
            hit = cur.fetchone()
            if hit:
                fk, conf = hit[0], 1.0
                unit = unit or hit[1]
            else:
                cur.execute(
                    """SELECT id, unit FROM metric_definitions
                       WHERE is_active AND name ILIKE %s
                       ORDER BY 1 LIMIT 1""",
                    (f"%{name}%",),
                )
                hit = cur.fetchone()
                if hit:
                    fk, conf = hit[0], 0.5
                    unit = unit or hit[1]
        normalised = {
            "profile_id": raw.get("profile_id"),
            "metric_definition_id": fk,
            "value": raw.get("value"),
            "unit": unit,
            "measured_at": raw.get("measured_at"),
            "source": raw.get("source", "contract"),
        }
        return {"fk": fk, "confidence": conf, "normalised": _drop_none(normalised)}

    if kind == "supplement":
        name = raw.get("name") or raw.get("extracted_name")
        fk, conf = None, 0.0
        if name:
            cur.execute(
                """SELECT supplement_id FROM supplement_aliases
                   WHERE lower(alias) = lower(%s) LIMIT 1""",
                (name,),
            )
            hit = cur.fetchone()
            if hit:
                fk, conf = hit[0], 1.0
            else:
                cur.execute(
                    "SELECT id FROM supplements WHERE is_active AND lower(name)=lower(%s) LIMIT 1",
                    (name,),
                )
                hit = cur.fetchone()
                if hit:
                    fk, conf = hit[0], 0.9
        normalised = {
            "profile_id": raw.get("profile_id"),
            "supplement_id": fk,
            "regimen_id": raw.get("regimen_id"),
            "taken_at": raw.get("taken_at"),
            "dose_amount": raw.get("dose_amount"),
            "dose_unit": raw.get("dose_unit"),
            "source": raw.get("source", "contract"),
        }
        return {"fk": fk, "confidence": conf, "normalised": _drop_none(normalised)}

    if kind == "journal":
        slug = raw.get("slug") or raw.get("behavior")
        fk, conf = None, 0.0
        if slug:
            cur.execute(
                "SELECT id FROM journal_behaviors WHERE is_active AND slug=%s LIMIT 1",
                (slug,),
            )
            hit = cur.fetchone()
            if hit:
                fk, conf = hit[0], 1.0
        normalised = {
            "profile_id": raw.get("profile_id"),
            "behavior_id": fk,
            "cycle_start": raw.get("cycle_start"),
            "answered_yes": raw.get("answered_yes"),
            "quantity": raw.get("quantity"),
            "unit": raw.get("unit"),
        }
        return {"fk": fk, "confidence": conf, "normalised": _drop_none(normalised)}

    if kind == "food":
        item = raw.get("description") or raw.get("item")
        fk, conf = None, 0.0
        if item:
            cur.execute(
                "SELECT id FROM food_guidance WHERE is_active AND lower(item)=lower(%s) LIMIT 1",
                (item,),
            )
            hit = cur.fetchone()
            if hit:
                fk, conf = hit[0], 1.0
        # food rows are valid even when unmatched to guidance; confidence floors
        # at 0.6 so a plain meal still writes to prod (guidance match is a bonus).
        conf = max(conf, 0.6)
        normalised = {k: v for k, v in raw.items() if k != "item"}
        return {"fk": fk, "confidence": conf, "normalised": _drop_none(normalised)}

    raise ValueError(f"unknown resolve kind: {kind!r}")


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# ----------------------------------------------------------------------------
# Step 2 — VALIDATE
# ----------------------------------------------------------------------------


def validate(conn, table: str, row: dict, attended: bool = True) -> tuple[bool, list]:
    """Check types, FK existence, and the TWO numeric ranges. Returns ``(ok, errors)``.

    ``attended`` (default True = a human confirms before write, e.g. the skill) only
    affects the unverifiable case — no plausible bound AND no prior history. Attended
    accepts it; unattended flags it ``unverified`` so the caller stages it.

    Never mutates ``row``. There are two distinct range concepts on
    metric_definitions — do not conflate them:

    * ``min_value`` / ``max_value`` = CLINICAL REFERENCE range. A breach is reported
      as ``out_of_range`` but is NOT a write blocker — the caller writes it to prod
      and surfaces it as *abnormal* (rule #8). High insulin / high android fat are
      real data the app exists to track.
    * ``plausible_min`` / ``plausible_max`` = PHYSIOLOGICALLY-POSSIBLE extremes (much
      wider). A value outside these is reported as ``implausible`` and IS a gate —
      the caller blocks the prod write and routes the row to staging for review
      (almost always an OCR / unit / comma-slip error). Where no plausibility bound
      is configured, fall back to a per-(profile, metric) heuristic: reject as
      implausible if the value is >10x the prior max or <0.1x the prior min for that
      profile+metric (and prior data exists). No prior data + no bound → accept.

    Hard failures (missing_required / fk_not_found / bad_type) are unchanged.
    """
    errors: list[dict] = []
    cur = conn.cursor()

    # required / not-null (no coercion of missing values)
    for col in _REQUIRED.get(table, []):
        if row.get(col) is None:
            errors.append({"code": "missing_required", "message": f"{col} is required"})

    # FK existence
    for col, ref in _FK_MAP.get(table, {}).items():
        val = row.get(col)
        if val is not None:
            cur.execute(f"SELECT 1 FROM {_ident(ref)} WHERE id = %s", (val,))
            if cur.fetchone() is None:
                errors.append(
                    {"code": "fk_not_found", "message": f"{col}={val!r} not in {ref}"}
                )

    # biomarkers: clinical-range (flag) + plausibility (gate), both off metric_definitions
    if (
        table == "biomarkers"
        and row.get("metric_definition_id") is not None
        and row.get("value") is not None
    ):
        cur.execute(
            "SELECT min_value, max_value, name, plausible_min, plausible_max "
            "FROM metric_definitions WHERE id = %s",
            (row["metric_definition_id"],),
        )
        md = cur.fetchone()
        if md:
            lo, hi, nm, plo, phi = md
            try:
                v = float(row["value"])
            except (TypeError, ValueError):
                errors.append(
                    {"code": "bad_type", "message": f"value {row['value']!r} is not numeric"}
                )
            else:
                # clinical reference range → flag (out_of_range), never gate
                if lo is not None and v < float(lo):
                    errors.append(
                        {"code": "out_of_range", "message": f"{nm} value {v} < min {lo}"}
                    )
                if hi is not None and v > float(hi):
                    errors.append(
                        {"code": "out_of_range", "message": f"{nm} value {v} > max {hi}"}
                    )
                # implausibility bound → gate (implausible) / staging
                errors.extend(
                    _implausible_errors(
                        cur, v, nm, plo, phi,
                        profile_id=row.get("profile_id"),
                        metric_id=row["metric_definition_id"],
                        attended=attended,
                    )
                )

    # food_logs: gate calories against the food_energy_kcal plausibility bound
    # (the root-cause backstop for the "1,020 kcal" -> 20 comma-slip). A missing
    # catalog row must NOT take down food logging — guard and skip if absent.
    if table == "food_logs" and row.get("calories") is not None:
        cur.execute(
            "SELECT plausible_min, plausible_max FROM metric_definitions "
            "WHERE name = 'food_energy_kcal' AND is_active",
        )
        md = cur.fetchone()
        if md is not None:
            plo, phi = md
            try:
                v = float(row["calories"])
            except (TypeError, ValueError):
                errors.append(
                    {"code": "bad_type", "message": f"calories {row['calories']!r} is not numeric"}
                )
            else:
                errors.extend(
                    _implausible_errors(cur, v, "food_energy_kcal", plo, phi)
                )

    return (len(errors) == 0, errors)


def _implausible_errors(cur, v: float, nm: str, plo, phi,
                        profile_id=None, metric_id=None, attended: bool = True) -> list:
    """Return gating errors if v cannot be trusted. Codes: ``implausible`` (outside
    an explicit bound or failing the heuristic) / ``unverified`` (unattended fail-safe).

    Resolution order:
      1. Explicit ``plausible_min``/``plausible_max`` set → value must fall within →
         else ``implausible``.
      2. No bound, but prior (profile, metric) history exists → 10x/0.1x heuristic →
         a gross outlier is ``implausible``.
      3. No bound AND no history → the value cannot be verified at all. ``attended``
         ingest (a human confirms before write — the skill) ACCEPTS it (returns []).
         UNATTENDED ingest must NOT silently accept an unverifiable value → it is
         flagged ``unverified`` so the caller stages it for review. This is the
         fail-safe: an automated path with no bound and no history has nothing
         catching an OCR/unit slip, so a human must look.
    """
    errs: list[dict] = []
    if plo is not None and v < float(plo):
        errs.append({"code": "implausible",
                     "message": f"{nm} value {v} < plausible_min {plo}"})
    if phi is not None and v > float(phi):
        errs.append({"code": "implausible",
                     "message": f"{nm} value {v} > plausible_max {phi}"})
    if errs or plo is not None or phi is not None:
        return errs  # an explicit bound governs; do not also run the heuristic

    # No explicit bound → fall back to the per-(profile, metric) heuristic.
    prior_max = prior_min = None
    if profile_id is not None and metric_id is not None:
        cur.execute(
            "SELECT max(value), min(value) FROM biomarkers "
            "WHERE profile_id = %s AND metric_definition_id = %s AND value IS NOT NULL",
            (profile_id, metric_id),
        )
        hist = cur.fetchone()
        if hist:
            prior_max, prior_min = hist

    if prior_max is None:
        # No bound AND no usable history — unverifiable.
        if not attended:
            errs.append({"code": "unverified",
                         "message": f"{nm} value {v}: no plausible bound and no prior "
                                    f"history; unattended ingest → staged for review"})
        return errs

    if v > 10.0 * float(prior_max):
        errs.append({"code": "implausible",
                     "message": f"{nm} value {v} > 10x prior max {prior_max} (no bound set)"})
    if prior_min is not None and float(prior_min) > 0 and v < 0.1 * float(prior_min):
        errs.append({"code": "implausible",
                     "message": f"{nm} value {v} < 0.1x prior min {prior_min} (no bound set)"})
    return errs


# ----------------------------------------------------------------------------
# Step 3 — WRITE (idempotent upsert) + staging insert
# ----------------------------------------------------------------------------


_GENERATED_CACHE: dict[str, frozenset] = {}


def _generated_cols(conn, table: str) -> frozenset:
    """Names of GENERATED ALWAYS columns on `table` (cached per process). These are
    computed by the DB — Postgres rejects any supplied value (`GeneratedAlways`), so
    write() must NEVER put them in the INSERT column list, even though they may be part
    of a unique index used as an ON CONFLICT target (e.g. supplement_intake_logs.taken_on,
    GENERATED from taken_at)."""
    if table not in _GENERATED_CACHE:
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT attname FROM pg_attribute
                   WHERE attrelid = ('public.' || %s)::regclass
                     AND attgenerated <> '' AND NOT attisdropped""",
                (table,),
            )
            _GENERATED_CACHE[table] = frozenset(r[0] for r in cur.fetchall())
        except Exception as exc:
            # introspection unavailable (e.g. a mocked conn in tests) — degrade to "none
            # generated" (old behaviour: insert every column). Don't cache the failure so
            # a real connection retries.
            logging.getLogger(__name__).warning(
                "_generated_cols introspection failed for %s: %s", table, exc
            )
            return frozenset()
    return _GENERATED_CACHE[table]


def write(conn, table: str, row: dict, conflict_cols) -> str:
    """Idempotent upsert: INSERT … ON CONFLICT (conflict_cols) DO UPDATE.

    Re-running with the same natural key UPDATES the existing row instead of
    inserting a duplicate. Returns 'inserted' | 'updated' | 'skipped'.

    GENERATED columns are dropped from the INSERT/UPDATE (the DB computes them) but may
    still serve as ON CONFLICT targets — so a conflict key like
    (profile_id, supplement_id, taken_on, source) works even though taken_on is generated.
    """
    generated = _generated_cols(conn, table)
    cols = [c for c in row.keys() if c not in generated]   # never write generated cols
    if not cols:
        raise ValueError("write(): empty row")
    conflict_cols = list(conflict_cols)
    for c in conflict_cols:
        # a generated conflict col need not be in `row` — the DB computes it for the index
        if c not in row and c not in generated:
            raise ValueError(f"write(): conflict column {c!r} not present in row")

    qcols = ", ".join(_ident(c) for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    conflict = ", ".join(_ident(c) for c in conflict_cols)
    # don't UPDATE conflict keys or generated columns
    update_cols = [c for c in cols if c not in conflict_cols]
    if update_cols:
        set_clause = ", ".join(f"{_ident(c)} = EXCLUDED.{_ident(c)}" for c in update_cols)
        do = f"DO UPDATE SET {set_clause}"
    else:
        do = "DO NOTHING"

    sql = (
        f"INSERT INTO {_ident(table)} ({qcols}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) {do} "
        f"RETURNING (xmax = 0) AS inserted"
    )
    cur = conn.cursor()
    cur.execute(sql, [row[c] for c in cols])
    res = cur.fetchone()
    if res is None:  # DO NOTHING hit an existing row
        return "skipped"
    return "inserted" if res[0] else "updated"


def stage(conn, table: str, source: dict, confidence, raw_text=None) -> "int|None":
    """Route a row to its stg_*_review queue (insert-only) for human review."""
    spec = _STG_SPEC.get(table)
    if spec is None:
        raise ValueError(f"no staging table mapped for {table!r}")
    stg_table, adapt = spec
    stg_row = _drop_none(adapt(source, confidence, raw_text))
    # Client-side id (no RETURNING): stg_*_review SELECT is maintainer-only (migration
    # 022), and INSERT … RETURNING would invoke that SELECT policy and fail for a
    # non-maintainer who is otherwise allowed to stage her own row.
    stg_id = str(_uuid.uuid4())
    stg_row["id"] = stg_id
    cols = list(stg_row.keys())
    qcols = ", ".join(_ident(c) for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO {_ident(stg_table)} ({qcols}) VALUES ({placeholders})"
    cur = conn.cursor()
    cur.execute(sql, [stg_row[c] for c in cols])
    return stg_id


# ----------------------------------------------------------------------------
# Step 4 — LOG (run + per-record errors)
# ----------------------------------------------------------------------------


def open_sync_log(conn, provider: str, method: str, *, profile_id=None,
                  sync_type="ingest", source_path=None) -> str:
    """Open a wearable_sync_log run row (status='in_progress') and return its id.

    The id is generated CLIENT-SIDE (not via RETURNING). Under the maintainer-only
    SELECT policy on wearable_sync_log (migration 022), INSERT … RETURNING would
    invoke the SELECT policy and fail for a non-maintainer (e.g. Dea) — even though
    she is allowed to insert. A client UUID avoids the read-back entirely.
    """
    sync_id = str(_uuid.uuid4())
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO wearable_sync_log
             (id, provider, method, sync_type, status, profile_id, source_path,
              records_in, records_upserted, records_skipped, records_failed, started_at)
           VALUES (%s, %s, %s, %s, 'in_progress', %s, %s, 0, 0, 0, 0, now())""",
        (sync_id, provider, method, sync_type, profile_id, source_path),
    )
    return sync_id


def close_sync_log(conn, sync_log_id, *, status="success", records_in=0,
                   records_upserted=0, records_skipped=0, records_failed=0) -> None:
    """Finalise a run row with counters + completed_at.

    Goes through the SECURITY DEFINER hs_close_sync_log() rather than a direct UPDATE:
    wearable_sync_log SELECT is maintainer-only (migration 022), and a plain
    UPDATE … WHERE id=… reads the row to locate it → a non-maintainer (e.g. Dea)
    would match 0 rows and never finalise her own run. The definer updates by id as
    owner (bypasses RLS); the caller legitimately holds the id from open_sync_log.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT public.hs_close_sync_log(%s, %s, %s, %s, %s, %s)",
        (sync_log_id, status, records_in, records_upserted, records_skipped, records_failed),
    )


def log_error(conn, sync_log_id, record_ref, code, msg, raw=None) -> None:
    """Record one per-record failure in wearable_sync_errors (raw is jsonb)."""
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO wearable_sync_errors
             (sync_log_id, record_ref, error_code, error_message, raw)
           VALUES (%s, %s, %s, %s, %s::jsonb)""",
        (sync_log_id, str(record_ref), str(code), str(msg), _as_jsonb_param(raw)),
    )


# ----------------------------------------------------------------------------
# Orchestrator — one record through the whole contract, atomically.
# ----------------------------------------------------------------------------


def ingest_record(conn, sync_log_id, kind, table, raw, conflict_cols,
                  record_ref=None, attended: bool = True) -> dict:
    """Run one record through resolve → validate → write|stage → log.

    Wrapped in a SAVEPOINT so a failure rolls back ONLY this record (the run
    continues). Gate order (PROMPT 18): parse → implausibility → confidence → write-or-stage:
      * IMPLAUSIBLE value (outside plausible_min/max, or failing the 10x/0.1x
        heuristic) → staging. This is the GATE: a physiologically-impossible value
        is almost always an OCR/unit/comma error, so it is blocked from prod. It is
        checked FIRST — ahead of the confidence gate.
      * UNVERIFIED value (no bound AND no history) on an UNATTENDED run → staging.
        Same gate: an automated path with nothing to check the value against must
        not silently accept it. (Attended runs accept — a human confirms the write.)
      * unresolved (fk None) or confidence < system_config threshold → staging
      * hard validation failure (missing_required/fk_not_found/bad_type) → wearable_sync_errors, status 'failed'
      * out-of-range numeric (CLINICAL reference range) → idempotent upsert to prod,
        flagged abnormal — NEVER gated; abnormal_labs derives LOW/HIGH at query time
      * otherwise → idempotent upsert to prod

    ``attended`` defaults True (the skill confirms before write). An automated /
    webhook caller passes attended=False to arm the unverified fail-safe.

    Returns ``{"status": "upserted"|"updated"|"skipped"|"staged"|"failed", ...}``.
    """
    cur = conn.cursor()
    if record_ref is None:
        record_ref = raw.get("source_log_path") or raw.get("measured_at") or repr(raw)[:120]
    raw_text = _as_jsonb_param(raw)

    cur.execute("SAVEPOINT hs_rec")
    try:
        r = resolve(conn, kind, raw)
        normalised = r["normalised"]
        conf = r["confidence"]
        source = {**raw, **normalised}
        threshold = confidence_min(conn)

        ok, errors = validate(conn, table, normalised, attended=attended)
        # Split the validation findings by how each is routed:
        gated = [e for e in errors if e["code"] in ("implausible", "unverified")]
        abnormal = [e for e in errors if e["code"] == "out_of_range"]
        hard = [e for e in errors
                if e["code"] not in ("implausible", "unverified", "out_of_range")]

        # GATE 1 — implausibility / unverified (checked before confidence). A value
        # outside the plausibility bound, or unverifiable on an unattended run, is
        # blocked from prod and staged for review.
        if gated:
            cur.execute("RELEASE SAVEPOINT hs_rec")
            stage(conn, table, source, conf, raw_text=raw_text)
            reason = ("implausible_value"
                      if any(e["code"] == "implausible" for e in gated)
                      else "unverified_unattended")
            return {"status": "staged", "reason": reason,
                    "confidence": conf, "range_flags": gated}

        # GATE 2 — confidence / unresolved. (Comes after implausibility; this also
        # catches the fk-None case before the hard-error check below, so an
        # unresolved metric is staged rather than failed.)
        if r["fk"] is None or conf < threshold:
            cur.execute("RELEASE SAVEPOINT hs_rec")
            stage(conn, table, source, conf, raw_text=raw_text)
            return {"status": "staged", "reason": "low_confidence_or_unresolved",
                    "confidence": conf}

        # Hard failures block the write (and the run continues — logged below).
        if hard:
            raise ContractError(f"validation failed for {table}", hard)

        # `out_of_range` is NOT a write blocker: metric_definitions.min/max are CLINICAL
        # REFERENCE ranges (for flagging), not plausibility bounds. A confidently-read
        # value outside the reference range is REAL DATA and exactly what this app tracks
        # (high insulin, high android fat, low vitamin D). It writes to prod; the
        # abnormal_labs view derives LOW/HIGH from min/max at query time.
        result = write(conn, table, normalised, conflict_cols)
        cur.execute("RELEASE SAVEPOINT hs_rec")
        out = {"status": result}
        if abnormal:
            # surface so the skill can flag it to the user at ingest time (the value is
            # stored verbatim; abnormal_labs shows the LOW/HIGH derivation)
            out["abnormal"] = True
            out["range_flags"] = abnormal
        return out

    except Exception as e:  # atomic per record: isolate, log, keep the run alive
        try:
            cur.execute("ROLLBACK TO SAVEPOINT hs_rec")
        except Exception:
            pass
        code = "validation" if isinstance(e, ContractError) else "exception"
        detail = getattr(e, "errors", None) or raw
        log_error(conn, sync_log_id, record_ref, code, str(e), detail)
        return {"status": "failed", "error": str(e),
                "errors": getattr(e, "errors", [])}
