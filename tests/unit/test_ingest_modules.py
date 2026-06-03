"""Unit tests for ingest/food.py, ingest/biomarker.py, ingest/supplement.py.

Three contract invariants under test:
  1. Known food item classifies and writes to food_logs (high-confidence LLM result).
  2. Unknown biomarker marker (no metric_definitions hit) stages to stg_biomarker_review.
  3. Supplement intake with an active regimen links the regimen_id in the prod row.

All DB calls are mocked — no live DB, no network calls.
The Anthropic classify_food() seam is patched to avoid network calls (rule #2 from CLAUDE.md).
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Shared UUIDs (never hardcoded in prod code — only in tests for fixture control)
# ---------------------------------------------------------------------------

PROFILE_UUID = str(uuid.uuid4())
METRIC_UUID = str(uuid.uuid4())
SUPPLEMENT_UUID = str(uuid.uuid4())
REGIMEN_UUID = str(uuid.uuid4())
GUIDANCE_UUID = str(uuid.uuid4())
FOOD_LOG_UUID = str(uuid.uuid4())
SUPPLEMENT_INTAKE_UUID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cursor(fetchone_results: list, fetchall_results=None):
    """Build a mock cursor that pops fetchone results from a list."""
    cur = MagicMock()
    results = list(fetchone_results)

    def _fetchone():
        return results.pop(0) if results else None

    cur.fetchone.side_effect = _fetchone
    cur.fetchall.return_value = fetchall_results or []
    return cur


def _conn_single_cursor(fetchone_results: list, fetchall_results=None):
    """A connection whose every cursor() call returns the same shared cursor."""
    cur = _make_cursor(fetchone_results, fetchall_results)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


# ===========================================================================
# Test 1 — Known food item classifies and writes to food_logs
# ===========================================================================


class TestKnownFoodClassifies:
    """A meal item that exists in food_guidance + high-confidence LLM result
    must be written to food_logs (INSERT), not staged."""

    def test_known_food_inserts_to_prod(self):
        """
        Call sequence for ingest_food_row with a known item:

          food.py:
            _lookup_guidance → food_guidance lookup per item
              fetchone: (GUIDANCE_UUID, "eggs", "green", "high protein", None)  ← hit

          ingest_food_row → classify_food() [PATCHED] → confidence 0.9 (> 0.7)

          confidence_min(conn) → system_config
              fetchone: (0.7,)

          validate(conn, "food_logs", row):
              _REQUIRED = ["profile_id", "log_date"] — both present → no errors
              FK: profile_id check
              fetchone: (1,)           ← profile exists
              (no metric_def FK for food_logs)
              calories=180 present → food_energy_kcal plausibility lookup
              fetchone: (25.0, 12000.0)   ← 180 is inside the bound → no gate

          INSERT INTO food_logs ... RETURNING id
              fetchone: (FOOD_LOG_UUID,)
        """
        fetchone_seq = [
            (GUIDANCE_UUID, "eggs", "green", "high protein", None),  # guidance hit
            (0.7,),                                                   # confidence_min
            (1,),                                                     # validate FK: profile_id
            (25.0, 12000.0),                                         # food_energy_kcal plausible bound
            (FOOD_LOG_UUID,),                                         # INSERT food_logs RETURNING id
        ]
        conn, cur = _conn_single_cursor(fetchone_seq)

        llm_response = {
            "verdict": "green",
            "confidence": 0.9,
            "calories": 180,
            "protein_g": 14.0,
            "carbs_g": 1.0,
            "fat_g": 13.0,
            "fiber_g": 0.0,
            "flags": ["high_protein", "low_carb"],
            "reasoning": "Eggs are a clean protein source.",
        }

        payload = {
            "meal_type": "breakfast",
            "meal_time": "08:00",
            "log_date": "2026-06-02",
            "location": "home",
            "items": ["eggs"],
        }

        with patch("ingest.food.classify_food", return_value=llm_response):
            from ingest.food import ingest_food_row
            result = ingest_food_row(payload, conn, PROFILE_UUID)

        assert result["status"] == "inserted", f"Expected 'inserted', got: {result}"
        assert result["confidence"] == 0.9

        # Verify INSERT went to food_logs (not stg_food_log_review)
        executed_sqls = [str(c) for c in cur.execute.call_args_list]
        food_log_inserts = [s for s in executed_sqls if "food_logs" in s and "INSERT" in s]
        stg_inserts = [s for s in executed_sqls if "stg_food_log_review" in s]
        assert len(food_log_inserts) >= 1, "Must INSERT into food_logs"
        assert len(stg_inserts) == 0, "Must NOT stage a high-confidence entry"

    def test_comma_slipped_calories_staged_through_real_path(self):
        """End-to-end through ingest_food_row (NOT just validate()): a meal logged
        as 20 kcal (the '1,020'->20 comma-slip) is below food_energy_kcal's
        plausible_min 25 → validate() flags it implausible → ingest_food_row routes
        it to stg_food_log_review, never to prod. High confidence (0.9) proves it is
        the BOUND that catches it, not low extraction confidence."""
        STG_UUID = str(uuid.uuid4())
        fetchone_seq = [
            (GUIDANCE_UUID, "eggs", "green", "high protein", None),  # guidance hit
            (0.7,),                                                   # confidence_min (0.9 > 0.7)
            (1,),                                                     # validate FK: profile_id
            (25.0, 12000.0),                                          # food_energy_kcal bound → 20 < 25
            (STG_UUID,),                                              # stage INSERT RETURNING id
        ]
        conn, cur = _conn_single_cursor(fetchone_seq)

        llm_response = {
            "verdict": "green", "confidence": 0.9,
            "calories": 20,  # comma-slip: should have been 1,020
            "protein_g": 14.0, "carbs_g": 1.0, "fat_g": 13.0, "fiber_g": 0.0,
            "flags": ["high_protein"], "reasoning": "Eggs.",
        }
        payload = {"meal_type": "breakfast", "log_date": "2026-06-02", "items": ["eggs"]}

        with patch("ingest.food.classify_food", return_value=llm_response):
            from ingest.food import ingest_food_row
            result = ingest_food_row(payload, conn, PROFILE_UUID)

        assert result["status"] == "staged", f"expected staged, got {result}"
        assert result["reason"] == "validation_failed"
        assert any(e["code"] == "implausible" for e in result.get("errors", []))
        # routed to staging, NOT to prod
        executed = [str(c) for c in cur.execute.call_args_list]
        assert any("stg_food_log_review" in s for s in executed), "must stage"
        assert not any("food_logs" in s and "INSERT" in s for s in executed), "must NOT write prod"


# ===========================================================================
# Test 2 — Unknown biomarker marker stages to stg_biomarker_review
# ===========================================================================


class TestUnknownMarkerStages:
    """A biomarker with a name that has no metric_definitions match must
    be staged to stg_biomarker_review, never written to biomarkers prod."""

    def test_unresolved_name_goes_to_staging(self):
        """
        Call sequence inside ingest_record(kind="biomarker"):

          contract.resolve("biomarker"):
            cur.execute: exact match SELECT id,unit FROM metric_definitions WHERE name=...
              fetchone: None   ← no exact match
            cur.execute: substring ILIKE match
              fetchone: None   ← no substring match
            → fk=None, confidence=0.0

          ingest_record sees fk=None → routes to stage() before confidence_min check:
            confidence_min(conn) → system_config
              fetchone: (0.7,)
            stage() → stg_biomarker_review INSERT RETURNING id
              fetchone: (999,)

        Note: contract.ingest_record calls open_sync_log/close_sync_log outside;
        here we call ingest_record directly (no sync log wrapper).
        """
        fetchone_seq = [
            None,     # resolve: exact name match miss
            None,     # resolve: ilike substring miss
            (0.7,),   # confidence_min
            (999,),   # stg_biomarker_review INSERT RETURNING id
        ]
        conn, cur = _conn_single_cursor(fetchone_seq)

        payload = {
            "profile_id": PROFILE_UUID,
            "name": "xyzzy_unknown_marker_9999",   # deliberately not in catalog
            "value": 42.0,
            "unit": "ng/mL",
            "measured_at": "2026-06-02T09:00:00+00:00",
            "source": "manual",
        }

        from lib.contract import ingest_record
        result = ingest_record(
            conn,
            sync_log_id=1,
            kind="biomarker",
            table="biomarkers",
            raw=payload,
            conflict_cols=["profile_id", "metric_definition_id", "measured_at"],
        )

        assert result["status"] == "staged", f"Expected 'staged', got: {result}"

        executed_sqls = [str(c) for c in cur.execute.call_args_list]
        stg_inserts = [s for s in executed_sqls if "stg_biomarker_review" in s]
        prod_inserts = [s for s in executed_sqls if '"biomarkers"' in s and "INSERT" in s]
        assert len(stg_inserts) >= 1, "Must INSERT into stg_biomarker_review"
        assert len(prod_inserts) == 0, "Must NOT write to biomarkers prod table"


# ===========================================================================
# Test 3 — Supplement intake links active regimen
# ===========================================================================


class TestIntakeLinksRegimen:
    """When no regimen_id is supplied in the payload, ingest_supplement_row
    must discover the active regimen from supplement_regimens and attach it
    in the production row written to supplement_intake_logs."""

    def test_active_regimen_attached_to_intake(self):
        """
        Call sequence inside ingest_supplement_row:

          contract.resolve("supplement") → supplement_aliases lookup
            fetchone: (SUPPLEMENT_UUID,)   ← alias exact match, confidence=1.0

          find_active_regimen(conn, profile_id, supplement_id):
            fetchone: (REGIMEN_UUID,)      ← active regimen found

          confidence_min(conn) → system_config:
            fetchone: (0.7,)               ← threshold; 1.0 ≥ 0.7 → go to prod

          validate(conn, "supplement_intake_logs", prod_row):
            _REQUIRED = ["profile_id", "supplement_id", "taken_at"] — all present
            FK: profile_id check
              fetchone: (1,)
            FK: supplement_id check
              fetchone: (1,)
            (no range check for supplement_intake_logs)

          contract.write() → INSERT supplement_intake_logs ... RETURNING (xmax=0)
            fetchone: (True,)              ← inserted
        """
        fetchone_seq = [
            (SUPPLEMENT_UUID,),  # resolve: supplement_aliases exact match
            (REGIMEN_UUID,),     # find_active_regimen
            (0.7,),              # confidence_min
            (1,),                # validate FK: profile_id
            (1,),                # validate FK: supplement_id
            (True,),             # write INSERT RETURNING (xmax=0) → inserted
        ]
        conn, cur = _conn_single_cursor(fetchone_seq)

        payload = {
            "name": "Berberine",        # will match via supplement_aliases
            "dose_amount": 500.0,
            "dose_unit": "mg",
            "taken_at": "2026-06-02T08:00:00+00:00",
            # regimen_id deliberately absent — must be auto-resolved
        }

        from ingest.supplement import ingest_supplement_row
        result = ingest_supplement_row(payload, conn, PROFILE_UUID)

        assert result["status"] == "inserted", f"Expected 'inserted', got: {result}"

        # Verify the regimen_id appeared in the INSERT SQL (not just in the row dict)
        executed_sqls = [str(c) for c in cur.execute.call_args_list]
        intake_inserts = [
            s for s in executed_sqls
            if "supplement_intake_logs" in s and "INSERT" in s
        ]
        assert len(intake_inserts) >= 1, "Must INSERT into supplement_intake_logs"

        # Check the values passed to the INSERT include the regimen UUID
        insert_calls = [
            c for c in cur.execute.call_args_list
            if "supplement_intake_logs" in str(c) and "INSERT" in str(c)
        ]
        assert len(insert_calls) >= 1
        insert_args = insert_calls[0][0]  # positional args: (sql, values)
        values = insert_args[1] if len(insert_args) > 1 else []
        assert REGIMEN_UUID in [str(v) for v in values], (
            f"regimen_id {REGIMEN_UUID} must appear in the INSERT values; got: {values}"
        )
