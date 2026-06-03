"""export/report_md.py — render any lib/views view (or a summary set) as markdown.

Consumes lib/views ONLY — no SQL here. The markdown is what the skill prints in chat.

  render_view(conn, name, profile_id, **params) -> markdown str
  render_summary(conn, profile_id) -> markdown str  (a daily/standing digest of key views)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.views import run_view, list_views, VIEWS


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _table(columns: list[str], rows: list[dict]) -> str:
    if not rows:
        return "_no rows_"
    head = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = "\n".join("| " + " | ".join(_fmt(r.get(c)) for c in columns) + " |" for r in rows)
    return "\n".join([head, sep, body])


def render_view(conn, name: str, profile_id: str, max_rows: int = 50, **params) -> str:
    """Render one named view as a markdown section."""
    res = run_view(conn, name, profile_id, **params)
    lines = [f"### {name}", f"_{res['description']}_", ""]
    if res.get("summary"):
        summ = " · ".join(f"**{k}**: {_fmt(v)}" for k, v in res["summary"].items())
        lines += [summ, ""]
    rows = res["rows"]
    note = ""
    if len(rows) > max_rows:
        note = f"\n_…{len(rows) - max_rows} more rows omitted_"
        rows = rows[:max_rows]
    lines.append(_table(res["columns"], rows) + note)
    return "\n".join(lines)


def render_summary(conn, profile_id: str) -> str:
    """A standing digest: recovery, sleep debt, abnormal labs, sprint, supplements."""
    parts = ["# HealthSpan summary", ""]
    for name in ("v_recovery_30d", "v_sleep_debt_30d", "abnormal_labs", "sprints_status", "v_india_vs_travel"):
        try:
            parts.append(render_view(conn, name, profile_id, max_rows=15))
            parts.append("")
        except Exception as e:
            parts.append(f"### {name}\n_error: {e}_\n")
    return "\n".join(parts)


def render_catalog() -> str:
    """List the available views (for the skill to show what it can report)."""
    lines = ["## Available reports", ""]
    for v in list_views():
        params = f"  (params: {', '.join(v['params'])})" if v["params"] else ""
        lines.append(f"- **{v['name']}** — {v['description']}{params}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse, os
    sys.path.insert(0, "scripts"); import hs_ops; hs_ops._load_env()
    import psycopg2
    ap = argparse.ArgumentParser()
    ap.add_argument("--view", help="view name; omit for the standing summary")
    ap.add_argument("--profile", default="21f69003-46f8-4e1c-a928-b1f694ce4aff")
    ap.add_argument("--marker", help="for v_biomarker_timeline")
    args = ap.parse_args()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    if args.view:
        kw = {"marker": args.marker} if args.marker else {}
        print(render_view(conn, args.view, args.profile, **kw))
    else:
        print(render_summary(conn, args.profile))
    conn.close()
