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
import re

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
    return float(get_config(conn, "ingest.confidence_min"))


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


def validate(conn, table: str, row: dict) -> tuple[bool, list]:
    """Check types, FK existence, and numeric range. Returns ``(ok, errors)``.

    Never mutates ``row``. Range checks use metric_definitions.min_value/max_value,
    which are CLINICAL REFERENCE ranges (for flagging), NOT plausibility bounds. An
    out-of-range value is reported (code ``out_of_range``) but is NOT a write blocker:
    the caller writes it to prod and surfaces it as abnormal. Only missing_required /
    fk_not_found / bad_type are hard failures.
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

    # numeric range (biomarkers): value vs the metric's configured min/max
    if (
        table == "biomarkers"
        and row.get("metric_definition_id") is not None
        and row.get("value") is not None
    ):
        cur.execute(
            "SELECT min_value, max_value, name FROM metric_definitions WHERE id = %s",
            (row["metric_definition_id"],),
        )
        md = cur.fetchone()
        if md:
            lo, hi, nm = md
            try:
                v = float(row["value"])
            except (TypeError, ValueError):
                errors.append(
                    {"code": "bad_type", "message": f"value {row['value']!r} is not numeric"}
                )
            else:
                if lo is not None and v < float(lo):
                    errors.append(
                        {"code": "out_of_range", "message": f"{nm} value {v} < min {lo}"}
                    )
                if hi is not None and v > float(hi):
                    errors.append(
                        {"code": "out_of_range", "message": f"{nm} value {v} > max {hi}"}
                    )

    return (len(errors) == 0, errors)


# ----------------------------------------------------------------------------
# Step 3 — WRITE (idempotent upsert) + staging insert
# ----------------------------------------------------------------------------


def write(conn, table: str, row: dict, conflict_cols) -> str:
    """Idempotent upsert: INSERT … ON CONFLICT (conflict_cols) DO UPDATE.

    Re-running with the same natural key UPDATES the existing row instead of
    inserting a duplicate. Returns 'inserted' | 'updated' | 'skipped'.
    """
    cols = list(row.keys())
    if not cols:
        raise ValueError("write(): empty row")
    conflict_cols = list(conflict_cols)
    for c in conflict_cols:
        if c not in row:
            raise ValueError(f"write(): conflict column {c!r} not present in row")

    qcols = ", ".join(_ident(c) for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    conflict = ", ".join(_ident(c) for c in conflict_cols)
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
    cols = list(stg_row.keys())
    qcols = ", ".join(_ident(c) for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    sql = (
        f"INSERT INTO {_ident(stg_table)} ({qcols}) VALUES ({placeholders}) RETURNING id"
    )
    cur = conn.cursor()
    cur.execute(sql, [stg_row[c] for c in cols])
    res = cur.fetchone()
    return res[0] if res else None


# ----------------------------------------------------------------------------
# Step 4 — LOG (run + per-record errors)
# ----------------------------------------------------------------------------


def open_sync_log(conn, provider: str, method: str, *, profile_id=None,
                  sync_type="ingest", source_path=None) -> "int":
    """Open a wearable_sync_log run row (status='running') and return its id."""
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO wearable_sync_log
             (provider, method, sync_type, status, profile_id, source_path,
              records_in, records_upserted, records_skipped, records_failed, started_at)
           VALUES (%s, %s, %s, 'in_progress', %s, %s, 0, 0, 0, 0, now())
           RETURNING id""",
        (provider, method, sync_type, profile_id, source_path),
    )
    return cur.fetchone()[0]


def close_sync_log(conn, sync_log_id, *, status="success", records_in=0,
                   records_upserted=0, records_skipped=0, records_failed=0) -> None:
    """Finalise a run row with counters + completed_at."""
    cur = conn.cursor()
    cur.execute(
        """UPDATE wearable_sync_log
              SET status = %s, records_in = %s, records_upserted = %s,
                  records_skipped = %s, records_failed = %s, completed_at = now()
            WHERE id = %s""",
        (status, records_in, records_upserted, records_skipped, records_failed, sync_log_id),
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
                  record_ref=None) -> dict:
    """Run one record through resolve → validate → write|stage → log.

    Wrapped in a SAVEPOINT so a failure rolls back ONLY this record (the run
    continues). Routing:
      * unresolved (fk None) or confidence < system_config threshold → staging
      * out-of-range numeric → idempotent upsert to prod, flagged abnormal (reference
        range is for flagging, not gating; abnormal_labs derives LOW/HIGH at query time)
      * hard validation failure (missing_required/fk_not_found/bad_type) → wearable_sync_errors, status 'failed'
      * otherwise → idempotent upsert to prod

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

        # route low-confidence / unresolved to staging
        if r["fk"] is None or conf < confidence_min(conn):
            cur.execute("RELEASE SAVEPOINT hs_rec")
            stage(conn, table, source, conf, raw_text=raw_text)
            return {"status": "staged", "reason": "low_confidence_or_unresolved",
                    "confidence": conf}

        ok, errors = validate(conn, table, normalised)
        # `out_of_range` is NOT a write blocker: metric_definitions.min/max are CLINICAL
        # REFERENCE ranges (for flagging), not plausibility bounds. A confidently-read
        # value outside the reference range is REAL DATA and exactly what this app tracks
        # (high insulin, high android fat, low vitamin D). It writes to prod; the
        # abnormal_labs view derives LOW/HIGH from min/max at query time. Routing it to
        # staging would starve that view. (Extraction-confidence staging stays above.)
        # Only HARD errors (missing_required / fk_not_found / bad_type) block the write.
        abnormal = [e for e in errors if e["code"] == "out_of_range"]
        hard = [e for e in errors if e["code"] != "out_of_range"]
        if hard:
            raise ContractError(f"validation failed for {table}", hard)

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
