"""Single source of truth for Claude model IDs used across HealthSpan.

Update the constants HERE when Anthropic retires a model — retired IDs return HTTP 404 and
silently break whatever path calls them. Call sites should read `system_config` first (e.g.
`drain.vision_model`, `brief.model`, `ingest.food_model`) and fall back to these constants, so
a model swap is a one-line change in the DB or here.

Last review 2026-06-09: `claude-3-5-haiku-20241022` RETIRED 2026-02-19 → `claude-haiku-4-5`.
(`ingest/food.py` had the retired id hardcoded — it would have 404'd on every call.)

POLICY (2026-06-09): **one model everywhere — SONNET.** Every path's fallback is `SONNET`; the
quality is uniform and the cost delta vs mixing in Haiku is ~$0.50/month (negligible — vision
dominates). `HAIKU` stays defined so you can cost-optimize a specific cheap path later WITHOUT a
code change — just set its `system_config` key (e.g. `brief.model = claude-haiku-4-5`).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Default everywhere (vision extraction, clustering, brief, food classify, screenshot OCR).
SONNET = "claude-sonnet-4-6"
# Available for a per-path cost override via system_config — not used by default.
HAIKU = "claude-haiku-4-5"


def is_model_error(exc: Exception) -> bool:
    """True if `exc` looks like a bad/retired model id (Anthropic returns 404 NotFoundError)."""
    name = type(exc).__name__
    return "NotFound" in name or "404" in str(exc) or "model:" in str(exc).lower()


def create_message(client, *, model: str, **kwargs):
    """`client.messages.create(...)` with a clear, actionable error when the model id is
    bad/retired. Without this, a retired model surfaces only as a raw 404 deep in a stack trace
    (or, worse, a silently-failed ingestion). Re-raises so callers never proceed on bad state."""
    try:
        return client.messages.create(model=model, **kwargs)
    except Exception as exc:  # noqa: BLE001 — re-raised below
        if is_model_error(exc):
            log.error(
                "MODEL ERROR: %r was rejected by the Anthropic API (likely retired or invalid). "
                "Fix the id in lib/models.py or the relevant system_config key. Original: %s",
                model, exc,
            )
        raise
