"""Daily health brief — sent after every food ingestion in run_once().

Sections (adult):
    Food    — kcal / protein / carbs / fat vs targets, remaining
    Supps   — taken vs active regimen by timing slot
    WHOOP   — latest recovery / HRV / RHR / sleep; stale flag if cycle_start < today
    Viome   — today's flagged (avoid/minimize) + superfood items from food_logs.verdict
    Actions — 2-4 concrete suggestions from Claude (haiku)

Minor-safe: growth / performance framing only; no deficit / restriction language.
CLI: python -m monitor.brief --profile-id <uuid>
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_BRIEF_MODEL = "claude-haiku-4-5-20251001"
_TIMING_ORDER = ["morning", "lunch", "dinner", "bedtime", "anytime"]


# ── data fetching ─────────────────────────────────────────────────────────────

def _fetch_food_full(db, profile_id: str, today: str) -> dict:
    """Full macro totals for today (kcal, protein, carbs, fat, meals)."""
    try:
        rows = db.select(
            "food_logs",
            select="calories,protein_g,carbs_g,fat_g",
            filters={
                "profile_id": f"eq.{profile_id}",
                "log_date": f"eq.{today}",
                "is_day_summary": "not.is.true",
            },
        )
        return {
            "kcal":      round(sum(float(r.get("calories") or 0) for r in rows)),
            "protein_g": round(sum(float(r.get("protein_g") or 0) for r in rows), 1),
            "carbs_g":   round(sum(float(r.get("carbs_g") or 0) for r in rows), 1),
            "fat_g":     round(sum(float(r.get("fat_g") or 0) for r in rows), 1),
            "meals":     len(rows),
        }
    except Exception:
        return {}


def _fetch_supplement_status(db, profile_id: str, today: str) -> list[dict]:
    """Active regimen entries with name, timing[], and whether taken today."""
    try:
        regimens = db.select(
            "supplement_regimens",
            select="supplement_id,timing",
            filters={
                "profile_id": f"eq.{profile_id}",
                "status": "eq.active",
                "start_date": f"lte.{today}",
                "or": f"(end_date.is.null,end_date.gte.{today})",
            },
        )
        if not regimens:
            return []

        supp_ids = list({str(r["supplement_id"]) for r in regimens})
        supp_names: dict[str, str] = {}
        for sid in supp_ids:
            rows = db.select(
                "supplements",
                select="id,name,display_name",
                filters={"id": f"eq.{sid}"},
                limit=1,
            )
            if rows:
                r = rows[0]
                supp_names[sid] = r.get("display_name") or r.get("name") or sid

        intakes = db.select(
            "supplement_intake_logs",
            select="supplement_id",
            filters={"profile_id": f"eq.{profile_id}", "taken_on": f"eq.{today}"},
        )
        taken_ids = {str(r["supplement_id"]) for r in intakes}

        result = []
        for r in regimens:
            sid = str(r["supplement_id"])
            timing = r.get("timing") or ["anytime"]
            if isinstance(timing, str):
                timing = [timing]
            result.append({
                "name":   supp_names.get(sid, sid),
                "timing": timing,
                "taken":  sid in taken_ids,
            })
        return result
    except Exception:
        return []


def _fetch_whoop(db, profile_id: str, today: str) -> dict:
    """Most recent WHOOP cycle. Marks stale if cycle_start date < today."""
    try:
        rows = db.select(
            "whoop_cycles",
            select="cycle_start,recovery_score_pct,hrv_ms,resting_hr_bpm,asleep_duration_min,sleep_performance_pct,energy_burned_cal",
            filters={"profile_id": f"eq.{profile_id}"},
            order="cycle_start.desc",
            limit=1,
        )
        if not rows:
            return {}
        r = rows[0]
        stale = (r.get("cycle_start") or "")[:10] < today
        return {
            "cycle_start":       r.get("cycle_start"),
            "recovery":          r.get("recovery_score_pct"),
            "hrv":               r.get("hrv_ms"),
            "rhr":               r.get("resting_hr_bpm"),
            "sleep_min":         r.get("asleep_duration_min"),
            "sleep_performance": r.get("sleep_performance_pct"),
            "energy_burned_cal": r.get("energy_burned_cal"),
            "stale":             stale,
        }
    except Exception:
        return {}


def _fetch_today_viome_flags(db, profile_id: str, today: str) -> list[dict]:
    """Today's food_logs rows with a non-null, non-clean verdict."""
    try:
        rows = db.select(
            "food_logs",
            select="description,verdict,flags",
            filters={
                "profile_id": f"eq.{profile_id}",
                "log_date": f"eq.{today}",
                "verdict": "not.is.null",
                "is_day_summary": "not.is.true",
            },
        )
        return [r for r in rows if r.get("verdict") and r.get("verdict") != "clean"]
    except Exception:
        return []


# ── formatting ────────────────────────────────────────────────────────────────

def _food_section(totals: dict, targets: dict, is_minor: bool, energy_burned: int | None = None) -> str:
    if not totals:
        return "Food: no data yet"
    kcal  = totals.get("kcal", 0)
    prot  = totals.get("protein_g", 0)
    carbs = totals.get("carbs_g", 0)
    fat   = totals.get("fat_g", 0)
    meals = totals.get("meals", 0)

    def _vs(got, target, unit: str) -> str:
        if target:
            remaining = max(0, float(target) - float(got))
            return f"{got}{unit} / {int(target)}{unit} ({int(remaining)} left)"
        return f"{got}{unit}"

    if is_minor:
        line = f"Food ({meals} entries): {kcal} kcal · {prot}g protein · {carbs}g carbs · {fat}g fat"
        t_cal = targets.get("daily_calories")
        if t_cal and kcal < 0.6 * float(t_cal):
            line += " — keep fuelling 💪"
        return line

    t_cal  = targets.get("daily_calories")
    t_prot = targets.get("protein_g")
    t_carbs = targets.get("carbs_g")
    t_fat  = targets.get("fat_g")
    lines = [
        f"Food ({meals} entries):",
        f"  Calories: {_vs(kcal, t_cal, ' kcal')}",
        f"  Protein:  {_vs(prot, t_prot, 'g')}",
        f"  Carbs:    {_vs(carbs, t_carbs, 'g')}",
        f"  Fat:      {_vs(fat, t_fat, 'g')}",
    ]
    if energy_burned and energy_burned > 0:
        deficit = energy_burned - kcal
        sign = "−" if deficit > 0 else "+"
        lines.append(f"  WHOOP burn: {energy_burned} kcal · net {sign}{abs(deficit)} kcal {'deficit' if deficit > 0 else 'surplus'}")
    return "\n".join(lines)


def _supps_section(supps: list[dict]) -> str:
    if not supps:
        return "Supplements: no active regimen"

    try:
        now_h = datetime.now(timezone.utc).hour
        if 5 <= now_h < 11:
            current_slot = "morning"
        elif 11 <= now_h < 15:
            current_slot = "lunch"
        elif 15 <= now_h < 20:
            current_slot = "dinner"
        else:
            current_slot = "bedtime"
    except Exception:
        current_slot = "morning"

    by_slot: dict[str, list[str]] = {s: [] for s in _TIMING_ORDER}
    for s in supps:
        for timing in (s.get("timing") or ["anytime"]):
            if timing in by_slot:
                mark = "✅" if s["taken"] else "⬜"
                by_slot[timing].append(f"{mark} {s['name']}")

    taken = sum(1 for s in supps if s["taken"])
    total = len(supps)
    lines = [f"Supplements ({taken}/{total} taken):"]
    for slot in _TIMING_ORDER:
        items = by_slot[slot]
        if items:
            marker = " ←" if slot == current_slot else ""
            lines.append(f"  {slot.capitalize()}{marker}: {', '.join(items)}")
    return "\n".join(lines)


def _whoop_section(whoop: dict, is_minor: bool) -> str:
    if not whoop:
        return "WHOOP: no data"
    rec       = whoop.get("recovery")
    hrv       = whoop.get("hrv")
    rhr       = whoop.get("rhr")
    sleep_min = whoop.get("sleep_min")
    stale     = whoop.get("stale", False)
    stale_tag = " ⚠️ (data >24h old)" if stale else ""

    parts = []
    if rec is not None:
        parts.append(f"Recovery {int(rec)}%")
    if hrv is not None:
        parts.append(f"HRV {round(float(hrv), 1)} ms")
    if rhr is not None:
        parts.append(f"RHR {int(rhr)} bpm")
    if sleep_min is not None:
        h, m = divmod(int(sleep_min), 60)
        parts.append(f"Sleep {h}h{m:02d}m")

    if not parts:
        return f"WHOOP{stale_tag}: no data"
    return f"WHOOP{stale_tag}: {' · '.join(parts)}"


def _viome_section(flags: list[dict]) -> str:
    avoids: list[str] = []
    minimizes: list[str] = []
    superfoods: list[str] = []
    for r in flags:
        desc    = r.get("description") or ""
        verdict = r.get("verdict") or ""
        items   = r.get("flags") or []
        targets_list = items if items else ([desc] if desc else [])
        if verdict == "avoid":
            avoids.extend(targets_list)
        elif verdict == "minimize":
            minimizes.extend(targets_list)
        elif verdict == "superfood":
            superfoods.extend(targets_list)

    parts = []
    if avoids:
        parts.append("⚠️ AVOID today: " + ", ".join(avoids))
    if minimizes:
        parts.append("⚠️ Minimize: " + ", ".join(minimizes))
    if superfoods:
        parts.append("✅ Superfoods: " + ", ".join(superfoods))
    return "\n".join(parts)


def _call_claude_actions(
    api_key: str,
    model: str,
    food_txt: str,
    supps_txt: str,
    whoop_txt: str,
    viome_txt: str,
    is_minor: bool,
) -> str:
    """Ask Claude for 2-4 concrete rest-of-day actions. Best-effort, empty string on failure."""
    try:
        import anthropic
        frame = (
            "a 13-year-old girl focused on growth and athletic performance"
            if is_minor
            else "an adult tracking health and longevity"
        )
        context = "\n".join(s for s in [food_txt, supps_txt, whoop_txt, viome_txt] if s)
        prompt = (
            f"You are a concise health coach for {frame}.\n"
            f"Their health data today:\n\n{context}\n\n"
            "Give exactly 2-4 concrete, specific actions for the rest of the day. "
            "Each action is one short sentence. Output a numbered list only — no preamble, no headers. "
            "Never use restriction or deficit language. Focus on what to DO."
        )
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        log.warning("Claude actions call failed: %s", exc)
        return ""


# ── public API ────────────────────────────────────────────────────────────────

def compose_brief(
    db,
    profile_id: str,
    cfg: dict,
    api_key: str,
    token: str,
    today: str,
) -> str:
    """Compose and send the daily brief for one adult profile. Returns the message text.

    Called from run_once() (best-effort, never raises) and by the CLI.
    """
    identity_rows = db.select(
        "telegram_identities",
        select="chat_id,is_minor,display_name",
        filters={"profile_id": f"eq.{profile_id}", "status": "eq.active"},
        limit=1,
    )
    if not identity_rows:
        log.warning("compose_brief: no active telegram_identity for profile %s", profile_id)
        return ""

    identity     = identity_rows[0]
    chat_id      = int(identity["chat_id"])
    is_minor     = bool(identity.get("is_minor"))
    display_name = (identity.get("display_name") or "").split()[0] or "there"

    try:
        from lib.context import load_context
        slug = (identity.get("display_name") or "").strip().lower()
        ctx  = load_context(slug) if slug else {}
    except Exception:
        ctx = {}
    targets = ctx.get("targets") or {}

    food        = _fetch_food_full(db, profile_id, today)
    supps       = _fetch_supplement_status(db, profile_id, today)
    whoop       = _fetch_whoop(db, profile_id, today)
    viome_flags = [] if is_minor else _fetch_today_viome_flags(db, profile_id, today)

    model      = str(cfg.get("brief.model", _BRIEF_MODEL)).strip('"')
    food_txt   = _food_section(food, targets, is_minor, energy_burned=whoop.get("energy_burned_cal"))
    supps_txt  = _supps_section(supps)
    whoop_txt  = _whoop_section(whoop, is_minor)
    viome_txt  = _viome_section(viome_flags)
    actions_txt = _call_claude_actions(
        api_key, model, food_txt, supps_txt, whoop_txt, viome_txt, is_minor,
    )

    sections = [f"*Daily brief — {display_name}*", food_txt, supps_txt, whoop_txt]
    if viome_txt:
        sections.append(viome_txt)
    if actions_txt:
        sections.append(f"Rest of day:\n{actions_txt}")
    msg = "\n\n".join(s for s in sections if s)

    from monitor.inbox_drain import telegram_send
    telegram_send(token, chat_id, msg)
    return msg


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    import os

    from lib.db_rest import DbRest, sign_in

    parser = argparse.ArgumentParser(description="Send a daily brief for a profile.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile-id", help="Send brief for a specific profile UUID")
    group.add_argument("--all", action="store_true", help="Send brief for all active adult profiles")
    parser.add_argument("--today", default=datetime.now(timezone.utc).date().isoformat())
    args = parser.parse_args()

    url  = os.environ["SUPABASE_URL"]
    anon = os.environ["SUPABASE_ANON_KEY"]
    jwt  = sign_in(url, anon, os.environ["HS_AUTH_EMAIL"], os.environ["HS_AUTH_PASSWORD"])
    with DbRest(url, anon, jwt) as db:
        cfg = {}
        try:
            rows = db.select("system_config", select="key,value")
            cfg = {r["key"]: r["value"] for r in rows}
        except Exception:
            pass

        api_key = os.environ["ANTHROPIC_API_KEY"]
        token   = os.environ["TELEGRAM_BOT_TOKEN"]

        if args.all:
            # Send to all active adult profiles that have a linked Telegram identity
            identities = db.select(
                "telegram_identities",
                select="profile_id",
                filters={"status": "eq.active", "is_minor": "eq.false"},
            )
            profile_ids = [r["profile_id"] for r in identities]
        else:
            profile_ids = [args.profile_id]

        for pid in profile_ids:
            msg = compose_brief(db, profile_id=pid, cfg=cfg, api_key=api_key, token=token, today=args.today)
            print(msg or f"(no brief sent for {pid})")


if __name__ == "__main__":
    main()
