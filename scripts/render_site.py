#!/usr/bin/env python3
"""
Regenerates docs/index.html from data/account_snapshots.json.

Pure local file processing, no network calls, so this step can't fail for
reasons outside its own control. Safe to run on every job even on a day
the ingest step above it found nothing new.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "account_snapshots.json"
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "docs" / "index.html"


def load_snapshots():
    if not DATA_FILE.exists():
        return {}
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def latest_by_platform(snapshots):
    latest = {}
    for record in snapshots.values():
        platform = record["platform"]
        if platform not in latest or record["date"] > latest[platform]["date"]:
            latest[platform] = record
    return latest


def metric_cards(record):
    if not record:
        return "<p class='muted'>No data yet.</p>"
    items = "".join(
        f"<div class='metric'><span class='value'>{v}</span>"
        f"<span class='label'>{k}</span></div>"
        for k, v in record["metrics"].items()
    )
    return f"<div class='metrics'>{items}</div>"


def table_rows(snapshots):
    rows = sorted(
        snapshots.values(), key=lambda r: (r["date"], r["platform"]), reverse=True
    )[:30]
    return "".join(
        f"<tr><td>{r['date']}</td><td>{r['platform']}</td>"
        f"<td>{json.dumps(r['metrics'])}</td></tr>"
        for r in rows
    )


def render(snapshots):
    latest = latest_by_platform(snapshots)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Leamington RFC — Social Dashboard</title>
<style>
  :root {{
    color-scheme: light dark;
    padding-top: env(safe-area-inset-top, 0px);
    padding-bottom: env(safe-area-inset-bottom, 0px);
  }}
  body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ margin-bottom: 0.2rem; }}
  .updated {{ color: #666; font-size: 0.9rem; margin-bottom: 2rem; }}
  h2 {{ margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }}
  .muted {{ color: #888; }}
  .metrics {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0; }}
  .metric {{ background: #f4f4f4; border-radius: 8px; padding: 0.8rem 1.2rem; min-width: 130px; }}
  .metric .value {{ display: block; font-size: 1.5rem; font-weight: bold; }}
  .metric .label {{ display: block; font-size: 0.8rem; color: #555; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 0.4rem; border-bottom: 1px solid #eee; }}
  @media (prefers-color-scheme: dark) {{
    .metric {{ background: #222; }}
    .metric .label {{ color: #aaa; }}
    th, td {{ border-color: #333; }}
  }}
</style>
</head>
<body>
  <h1>Leamington RFC — Social Dashboard</h1>
  <p class="updated">Last updated {generated_at}</p>

  <h2>Facebook — latest</h2>
  {metric_cards(latest.get('facebook'))}

  <h2>Instagram — latest</h2>
  {metric_cards(latest.get('instagram'))}

  <h2>Recent snapshots</h2>
  <table>
    <thead><tr><th>Date</th><th>Platform</th><th>Metrics</th></tr></thead>
    <tbody>{table_rows(snapshots)}</tbody>
  </table>
</body>
</html>"""


def main():
    snapshots = load_snapshots()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(render(snapshots), encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
