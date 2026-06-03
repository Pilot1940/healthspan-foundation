"""export/dashboard.py — self-contained HTML dashboard (snapshot-at-render).

Design decision (2026-06-03): SNAPSHOT, not live-query. The data is fetched via
lib/views at render time and baked into the HTML as JSON, so the file is truly
self-contained — opens anywhere (Dropbox, email), carries NO DB credential, and
nothing leaks if it's synced. "Freshness" = re-running the emitter. Charts render
client-side from the embedded JSON via Chart.js (CDN).

Consumes lib/views ONLY.

  build_dashboard(conn, profile_id, out_path, display_name) -> path
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.views import run_view


def _default(o):
    # JSON-encode Decimals / dates from psycopg2
    from decimal import Decimal
    import datetime as _dt
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (_dt.date, _dt.datetime)):
        return o.isoformat()
    return str(o)


def build_dashboard(conn, profile_id: str, out_path: str, display_name: str = "HealthSpan") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rec = run_view(conn, "v_recovery_30d", profile_id)
    sleep = run_view(conn, "v_sleep_debt_30d", profile_id)
    zones = run_view(conn, "v_workout_zone_summary", profile_id)
    labs = run_view(conn, "abnormal_labs", profile_id)
    sprint = run_view(conn, "sprints_status", profile_id)
    vo2 = run_view(conn, "vo2_status", profile_id)

    payload = {
        "recovery": rec["rows"], "recovery_summary": rec.get("summary", {}),
        "sleep": sleep["rows"], "zones": zones["rows"],
        "labs": labs["rows"], "sprints": sprint["rows"],
        "vo2": vo2["rows"], "vo2_summary": vo2.get("summary", {}),
    }
    data_json = json.dumps(payload, default=_default)

    rs = rec.get("summary", {})
    active_sprint = next((s["name"] for s in sprint["rows"] if s["status"] == "active"), "—")
    kpi = {
        "avg_recovery": rs.get("avg_recovery", "—"),
        "avg_hrv": rs.get("avg_hrv", "—"),
        "avg_rhr": rs.get("avg_rhr", "—"),
        "no_sleep_days": rs.get("no_sleep_days", "—"),
        "abnormal_count": len(labs["rows"]),
        "active_sprint": active_sprint,
    }

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{display_name} — Health Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body{{font-family:system-ui,-apple-system,sans-serif;margin:0;background:#f4f6f7;color:#222}}
  header{{background:#2c3e50;color:#fff;padding:1.2rem 2rem}}
  header h1{{margin:0;font-size:1.4rem}} header .stamp{{opacity:.7;font-size:.85rem}}
  .wrap{{max-width:1100px;margin:1.5rem auto;padding:0 1rem}}
  .kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin-bottom:1.5rem}}
  .kpi{{background:#fff;border-radius:10px;padding:1rem;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
  .kpi .v{{font-size:1.8rem;font-weight:700;color:#2c3e50}} .kpi .l{{font-size:.8rem;color:#777;text-transform:uppercase;letter-spacing:.04em}}
  .card{{background:#fff;border-radius:10px;padding:1.2rem;margin-bottom:1.5rem;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
  .card h2{{margin:0 0 .8rem;font-size:1.05rem;color:#2c3e50}}
  table{{border-collapse:collapse;width:100%;font-size:.85rem}} th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #eee}}
  th{{color:#777;font-weight:600}} .flag-HIGH{{color:#c0392b;font-weight:600}} .flag-LOW{{color:#2980b9;font-weight:600}}
  .note{{font-size:.78rem;color:#999;margin-top:.4rem}}
</style></head><body>
<header><h1>{display_name} — Health Dashboard</h1><div class="stamp">snapshot {stamp} · regenerate to refresh</div></header>
<div class="wrap">
  <div class="kpis">
    <div class="kpi"><div class="v">{kpi['avg_recovery']}%</div><div class="l">Avg Recovery 30d</div></div>
    <div class="kpi"><div class="v">{kpi['avg_hrv']}</div><div class="l">Avg HRV (ms)</div></div>
    <div class="kpi"><div class="v">{kpi['avg_rhr']}</div><div class="l">Avg RHR (bpm)</div></div>
    <div class="kpi"><div class="v">{kpi['abnormal_count']}</div><div class="l">Abnormal labs</div></div>
    <div class="kpi"><div class="v">{kpi['no_sleep_days']}</div><div class="l">No-sleep days 30d</div></div>
    <div class="kpi"><div class="v" style="font-size:1rem">{kpi['active_sprint']}</div><div class="l">Active sprint</div></div>
  </div>
  <div class="card"><h2>Recovery (30d) — no-sleep days skipped, not zeroed</h2><canvas id="recovery"></canvas></div>
  <div class="card"><h2>Sleep: asleep vs need (30d)</h2><canvas id="sleep"></canvas></div>
  <div class="card"><h2>Workout time-in-zone</h2><canvas id="zones"></canvas></div>
  <div class="card"><h2>Abnormal labs (latest per marker)</h2><div id="labs"></div><div class="note">Flags vs reference range; recency varies — check date column.</div></div>
</div>
<script>
const D = {data_json};
const C = (id,cfg)=>new Chart(document.getElementById(id),cfg);
// recovery: only scored points (null recovery = no sleep, shown as gaps)
C('recovery',{{type:'line',data:{{labels:D.recovery.map(r=>r.date),datasets:[
  {{label:'Recovery %',data:D.recovery.map(r=>r.recovery_score_pct),borderColor:'#27ae60',spanGaps:false}},
  {{label:'HRV',data:D.recovery.map(r=>r.hrv_ms),borderColor:'#2980b9',yAxisID:'y1'}}
]}},options:{{scales:{{y1:{{position:'right',grid:{{drawOnChartArea:false}}}}}}}}}});
C('sleep',{{type:'line',data:{{labels:D.sleep.map(r=>r.date),datasets:[
  {{label:'Asleep (min)',data:D.sleep.map(r=>r.asleep_duration_min),borderColor:'#8e44ad'}},
  {{label:'Need (min)',data:D.sleep.map(r=>r.sleep_need_min),borderColor:'#bdc3c7',borderDash:[5,5]}}
]}}}});
C('zones',{{type:'bar',data:{{labels:D.zones.map(r=>r.date+' '+r.activity_name),datasets:[
  {{label:'Z2',data:D.zones.map(r=>r.z2_min),backgroundColor:'#aed6f1'}},
  {{label:'Z3',data:D.zones.map(r=>r.z3_min),backgroundColor:'#f9e79f'}},
  {{label:'Z4',data:D.zones.map(r=>r.z4_min),backgroundColor:'#f5b041'}},
  {{label:'Z5',data:D.zones.map(r=>r.z5_min),backgroundColor:'#e74c3c'}}
]}},options:{{scales:{{x:{{stacked:true}},y:{{stacked:true}}}}}}}});
// labs table
document.getElementById('labs').innerHTML = D.labs.length ? (
 '<table><tr><th>Marker</th><th>Value</th><th>Ref</th><th>Flag</th><th>Date</th></tr>'+
 D.labs.map(r=>`<tr><td>${{r.marker}}</td><td>${{r.qualifier||''}}${{r.value}} ${{r.unit||''}}</td>`+
 `<td>${{r.min_value??''}}–${{r.max_value??''}}</td><td class="flag-${{r.flag}}">${{r.flag}}</td><td>${{r.measured_at}}</td></tr>`).join('')+'</table>'
) : '<i>no abnormal labs</i>';
</script>
</body></html>"""

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(html)
    return out_path


if __name__ == "__main__":
    import argparse, os
    sys.path.insert(0, "scripts"); import hs_ops; hs_ops._load_env()
    import psycopg2
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="21f69003-46f8-4e1c-a928-b1f694ce4aff")
    ap.add_argument("--name", default="PC")
    ap.add_argument("--out", default="/tmp/healthspan-dashboard.html")
    args = ap.parse_args()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    print("Wrote:", build_dashboard(conn, args.profile, args.out, args.name))
    conn.close()
