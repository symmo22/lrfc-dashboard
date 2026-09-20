#!/usr/bin/env python3
"""
Regenerates docs/index.html from data/account_snapshots.json and
data/posts.json.

Pure local file processing, no network calls, so this step can't fail for
reasons outside its own control. Safe to run on every job even on a day
the ingest steps above it found nothing new.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ACCOUNT_FILE = DATA_DIR / "account_snapshots.json"
POSTS_FILE = DATA_DIR / "posts.json"
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "docs" / "index.html"


def load_json(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


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


def account_table_rows(snapshots):
    rows = sorted(
        snapshots.values(), key=lambda r: (r["date"], r["platform"]), reverse=True
    )[:30]
    return "".join(
        f"<tr><td>{r['date']}</td><td>{r['platform']}</td>"
        f"<td>{json.dumps(r['metrics'])}</td></tr>"
        for r in rows
    )


def latest_post_metrics(record):
    snapshots = record.get("snapshots", {})
    if not snapshots:
        return {}
    latest_date = max(snapshots.keys())
    return snapshots[latest_date]


def post_reach(record):
    metrics = latest_post_metrics(record)
    return metrics.get("reach") or metrics.get("post_total_media_view_unique") or 0


def post_engagement_rate(record):
    metrics = latest_post_metrics(record)
    reach = post_reach(record)
    if not reach:
        return None
    if record["platform"] == "instagram":
        interactions = metrics.get("total_interactions")
    else:
        static = record.get("static_metrics", {})
        interactions = (static.get("reactions") or 0) + (static.get("comments") or 0) + (
            static.get("shares") or 0
        )
    if interactions is None:
        return None
    return round(100 * interactions / reach, 1)


def posts_table_rows(posts, limit=20):
    with_metrics = [p for p in posts.values() if p.get("snapshots")]
    ranked = sorted(
        with_metrics,
        key=lambda p: (post_engagement_rate(p) or -1),
        reverse=True,
    )[:limit]

    if not ranked:
        return "<tr><td colspan='6' class='muted'>No post-level data yet.</td></tr>"

    def row(p):
        rate = post_engagement_rate(p)
        rate_str = f"{rate}%" if rate is not None else "—"
        caption = (p.get("caption") or "")[:60]
        if len(p.get("caption") or "") > 60:
            caption += "…"
        link = p.get("permalink") or "#"
        return (
            f"<tr><td>{p['published_at'][:10]}</td><td>{p['platform']}</td>"
            f"<td>{p['format']}</td><td>{post_reach(p)}</td><td>{rate_str}</td>"
            f"<td><a href='{link}' target='_blank' rel='noopener'>{caption}</a></td></tr>"
        )

    return "".join(row(p) for p in ranked)


def render(account_snapshots, posts):
    latest = latest_by_platform(account_snapshots)
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
  .table-wrap {{ overflow-x: auto; }}
  a {{ color: inherit; }}
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

  <h2>Recent posts, best performing first</h2>
  <p class="muted">Ranked by engagement rate (interactions ÷ reach). Only posts from the last 35 days are tracked.</p>
  <div class="table-wrap">
  <table>
    <thead><tr><th>Date</th><th>Platform</th><th>Format</th><th>Reach</th><th>Eng. rate</th><th>Caption</th></tr></thead>
    <tbody>{posts_table_rows(posts)}</tbody>
  </table>
  </div>

  <h2>Recent account snapshots</h2>
  <div class="table-wrap">
  <table>
    <thead><tr><th>Date</th><th>Platform</th><th>Metrics</th></tr></thead>
    <tbody>{account_table_rows(account_snapshots)}</tbody>
  </table>
  </div>
</body>
</html>"""


def main():
    account_snapshots = load_json(ACCOUNT_FILE)
    posts = load_json(POSTS_FILE)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(render(account_snapshots, posts), encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
