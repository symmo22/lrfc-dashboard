#!/usr/bin/env python3
"""
Regenerates docs/index.html from data/account_snapshots.json and
data/posts.json.

Pure local file processing, no network calls, so this step can't fail for
reasons outside its own control. Safe to run on every job even on a day
the ingest steps above it found nothing new.

v3: replaces the raw-JSON account snapshot table with an actual follower
growth chart, and adds a top-posts-by-reach bar chart above the detail
table. Chart.js loaded from a CDN - fine here since this is a plain
GitHub Pages site with no content-security restrictions, unlike a
published Claude artifact.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_posts import LOOKBACK_DAYS  # noqa: E402 - single source of truth

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


def follower_growth_series(snapshots):
    """One point per day per platform, for the growth chart. Uses whichever
    follower-count field that platform's snapshot has (fan_count for
    Facebook, followers_count for Instagram)."""
    by_platform = {"facebook": {}, "instagram": {}}
    for record in snapshots.values():
        platform = record["platform"]
        metrics = record.get("metrics", {})
        count = metrics.get("fan_count") if platform == "facebook" else metrics.get("followers_count")
        if count is not None:
            by_platform[platform][record["date"]] = count

    all_dates = sorted({d for platform in by_platform.values() for d in platform})
    fb_series = [by_platform["facebook"].get(d) for d in all_dates]
    ig_series = [by_platform["instagram"].get(d) for d in all_dates]
    return all_dates, fb_series, ig_series


def latest_post_metrics(record):
    snaps = record.get("snapshots", {})
    if not snaps:
        return {}
    return snaps[max(snaps.keys())]


def post_reach(record):
    metrics = latest_post_metrics(record)
    return metrics.get("reach") or metrics.get("post_total_media_view_unique") or 0


def post_views(record):
    metrics = latest_post_metrics(record)
    return metrics.get("views") or metrics.get("post_media_view") or 0


def post_counts(record):
    static = record.get("static_metrics", {})
    metrics = latest_post_metrics(record)
    if record["platform"] == "instagram":
        return static.get("likes"), static.get("comments"), metrics.get("shares"), metrics.get("saved")
    return static.get("reactions"), static.get("comments"), static.get("shares"), None


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


def fmt(value):
    return "—" if value is None else value


def top_posts_for_chart(posts, limit=8):
    with_metrics = [p for p in posts.values() if p.get("snapshots")]
    ranked = sorted(with_metrics, key=post_reach, reverse=True)[:limit]
    labels, values = [], []
    for p in ranked:
        caption = (p.get("caption") or p["format"])[:40].replace("\n", " ")
        labels.append(f"{caption} ({p['platform'][:2].upper()})")
        values.append(post_reach(p))
    return labels, values


def posts_table_rows(posts, limit=20):
    with_metrics = [p for p in posts.values() if p.get("snapshots")]
    ranked = sorted(with_metrics, key=post_reach, reverse=True)[:limit]

    if not ranked:
        return "<tr><td colspan='10' class='muted'>No post-level data yet.</td></tr>"

    def row(p):
        rate = post_engagement_rate(p)
        rate_str = f"{rate}%" if rate is not None else "—"
        likes, comments, shares, saves = post_counts(p)
        caption = (p.get("caption") or "")[:60]
        if len(p.get("caption") or "") > 60:
            caption += "…"
        link = p.get("permalink") or "#"
        return (
            f"<tr><td>{p['published_at'][:10]}</td><td>{p['platform']}</td>"
            f"<td>{p['format']}</td><td>{post_reach(p)}</td><td>{post_views(p)}</td>"
            f"<td>{fmt(likes)}</td><td>{fmt(comments)}</td><td>{fmt(shares)}</td>"
            f"<td>{fmt(saves)}</td><td>{rate_str}</td>"
            f"<td><a href='{link}' target='_blank' rel='noopener'>{caption}</a></td></tr>"
        )

    return "".join(row(p) for p in ranked)


def render(account_snapshots, posts):
    latest = latest_by_platform(account_snapshots)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    growth_dates, fb_growth, ig_growth = follower_growth_series(account_snapshots)
    top_labels, top_values = top_posts_for_chart(posts)

    has_growth_history = len(growth_dates) > 1
    has_top_posts = len(top_labels) > 0

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Leamington RFC — Social Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root {{
    color-scheme: light dark;
    padding-top: env(safe-area-inset-top, 0px);
    padding-bottom: env(safe-area-inset-bottom, 0px);
  }}
  body {{ font-family: system-ui, sans-serif; max-width: 1000px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ margin-bottom: 0.2rem; }}
  .updated {{ color: #666; font-size: 0.9rem; margin-bottom: 2rem; }}
  h2 {{ margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }}
  .muted {{ color: #888; }}
  .metrics {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0; }}
  .metric {{ background: #f4f4f4; border-radius: 8px; padding: 0.8rem 1.2rem; min-width: 130px; }}
  .metric .value {{ display: block; font-size: 1.5rem; font-weight: bold; }}
  .metric .label {{ display: block; font-size: 0.8rem; color: #555; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 0.4rem; border-bottom: 1px solid #eee; white-space: nowrap; }}
  .table-wrap {{ overflow-x: auto; }}
  .chart-wrap {{ position: relative; height: 320px; margin: 1rem 0 2rem; }}
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

  <h2>Follower growth</h2>
  {"<div class='chart-wrap'><canvas id='growthChart'></canvas></div>" if has_growth_history else "<p class='muted'>Not enough history yet — this fills in day by day as the pipeline keeps running.</p>"}

  <h2>Best performing posts</h2>
  {"<div class='chart-wrap'><canvas id='topPostsChart'></canvas></div>" if has_top_posts else "<p class='muted'>No post data yet.</p>"}

  <h2>All recent posts</h2>
  <p class="muted">Posts from the last {LOOKBACK_DAYS} days, furthest reaching first. Eng. rate = interactions ÷ reach.</p>
  <div class="table-wrap">
  <table>
    <thead><tr><th>Date</th><th>Platform</th><th>Format</th><th>Reach</th><th>Views</th>
    <th>Likes</th><th>Comments</th><th>Shares</th><th>Saves</th><th>Eng. rate</th><th>Caption</th></tr></thead>
    <tbody>{posts_table_rows(posts)}</tbody>
  </table>
  </div>

<script>
{"const growthCtx = document.getElementById('growthChart');" if has_growth_history else ""}
{f'''new Chart(growthCtx, {{
  type: 'line',
  data: {{
    labels: {json.dumps(growth_dates)},
    datasets: [
      {{ label: 'Facebook fans', data: {json.dumps(fb_growth)}, borderColor: '#3E3787', tension: 0.2, spanGaps: true }},
      {{ label: 'Instagram followers', data: {json.dumps(ig_growth)}, borderColor: '#CF3B41', tension: 0.2, spanGaps: true }}
    ]
  }},
  options: {{ responsive: true, maintainAspectRatio: false, scales: {{ y: {{ beginAtZero: false }} }} }}
}});''' if has_growth_history else ""}

{"const topCtx = document.getElementById('topPostsChart');" if has_top_posts else ""}
{f'''new Chart(topCtx, {{
  type: 'bar',
  data: {{
    labels: {json.dumps(top_labels)},
    datasets: [{{ label: 'Reach', data: {json.dumps(top_values)}, backgroundColor: '#3E3787' }}]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }}
  }}
}});''' if has_top_posts else ""}
</script>
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
