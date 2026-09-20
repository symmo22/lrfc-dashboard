#!/usr/bin/env python3
"""
Regenerates docs/index.html from data/account_snapshots.json and
data/posts.json.

v4: full brand redesign against the real Leamington RFC brand guidelines
(colours, Supreme typeface, centenary crest) rather than generic styling.

Historical anchors, honesty note:
HISTORICAL_ANCHORS below holds specific, dated follower counts Mark gave
directly (not fetched by any script, not estimated). These render as a
separate "then vs now" callout, NOT as a point plotted on the daily line
chart. Meta's API has no historical follower data at all, so the only
honest way to show "how far the account has come" is to state the two
real numbers and the real gap between their dates - not draw a line that
would visually claim daily measurement across months nothing was
actually tracked. The line chart stays scoped to what was genuinely
measured, day by day, since the pipeline went live.
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
CREST_B64_FILE = Path(__file__).resolve().parent / "crest_b64.txt"

# Manually provided by Mark in chat, not sourced by any script. Add
# Instagram's here the same way once he confirms the exact date he took
# over - it's currently missing on purpose rather than guessed.
HISTORICAL_ANCHORS = {
    "facebook": {"date": "2025-08-01", "count": 1800, "approx": True},
    # "instagram": {"date": "YYYY-MM-DD", "count": 1043, "approx": False},
}

BRAND = {
    "blue": "#3E3787",
    "yellow": "#FAE226",
    "red": "#CF3B41",
    "green": "#60AC3F",
    "white": "#ffffff",
}


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


def previous_snapshot(snapshots, platform, before_date):
    candidates = [
        r for r in snapshots.values()
        if r["platform"] == platform and r["date"] < before_date
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r["date"])


def follower_count(record, platform):
    if not record:
        return None
    metrics = record.get("metrics", {})
    return metrics.get("fan_count") if platform == "facebook" else metrics.get("followers_count")


def delta_badge(current, previous):
    if current is None or previous is None:
        return ""
    diff = current - previous
    if diff == 0:
        return "<span class='delta flat'>no change</span>"
    arrow = "▲" if diff > 0 else "▼"
    cls = "up" if diff > 0 else "down"
    return f"<span class='delta {cls}'>{arrow} {abs(diff)} since yesterday</span>"


def platform_hero(snapshots, platform, label, color):
    latest = latest_by_platform(snapshots).get(platform)
    if not latest:
        return f"<div class='hero-card' style='border-top-color:{color}'><h3>{label}</h3><p class='muted'>No data yet.</p></div>"
    prev = previous_snapshot(snapshots, platform, latest["date"])
    current_count = follower_count(latest, platform)
    prev_count = follower_count(prev, platform)
    metrics = latest.get("metrics", {})
    reach = metrics.get("page_total_media_view_unique") or metrics.get("reach") or 0

    anchor_html = ""
    anchor = HISTORICAL_ANCHORS.get(platform)
    if anchor and current_count:
        growth_pct = round(100 * (current_count - anchor["count"]) / anchor["count"])
        approx = "~" if anchor.get("approx") else ""
        anchor_date = datetime.fromisoformat(anchor["date"]).strftime("%B %Y")
        sign = "+" if growth_pct >= 0 else ""
        anchor_html = (
            f"<p class='story'>{approx}{anchor['count']:,} \u2192 {current_count:,} "
            f"since {anchor_date} &nbsp;<strong class='story-pct'>{sign}{growth_pct}%</strong></p>"
        )

    return f"""<div class='hero-card' style='border-top-color:{color}'>
      <h3>{label}</h3>
      <div class='hero-number'>{current_count:,}</div>
      <div class='hero-label'>followers {delta_badge(current_count, prev_count)}</div>
      <div class='hero-sub'>{reach:,} reach, latest day</div>
      {anchor_html}
    </div>"""


def follower_growth_series(snapshots):
    by_platform = {"facebook": {}, "instagram": {}}
    for record in snapshots.values():
        platform = record["platform"]
        count = follower_count(record, platform)
        if count is not None:
            by_platform[platform][record["date"]] = count
    all_dates = sorted({d for p in by_platform.values() for d in p})
    fb = [by_platform["facebook"].get(d) for d in all_dates]
    ig = [by_platform["instagram"].get(d) for d in all_dates]
    return all_dates, fb, ig


def latest_post_metrics(record):
    snaps = record.get("snapshots", {})
    return snaps[max(snaps.keys())] if snaps else {}


def post_reach(record):
    m = latest_post_metrics(record)
    return m.get("reach") or m.get("post_total_media_view_unique") or 0


def post_views(record):
    m = latest_post_metrics(record)
    return m.get("views") or m.get("post_media_view") or 0


def post_counts(record):
    static = record.get("static_metrics", {})
    m = latest_post_metrics(record)
    if record["platform"] == "instagram":
        return static.get("likes"), static.get("comments"), m.get("shares"), m.get("saved")
    return static.get("reactions"), static.get("comments"), static.get("shares"), None


def post_engagement_rate(record):
    m = latest_post_metrics(record)
    reach = post_reach(record)
    if not reach:
        return None
    if record["platform"] == "instagram":
        interactions = m.get("total_interactions")
    else:
        static = record.get("static_metrics", {})
        interactions = (static.get("reactions") or 0) + (static.get("comments") or 0) + (static.get("shares") or 0)
    return None if interactions is None else round(100 * interactions / reach, 1)


def fmt(value):
    return "—" if value is None else value


def top_posts_for_chart(posts, limit=8):
    with_metrics = [p for p in posts.values() if p.get("snapshots")]
    ranked = sorted(with_metrics, key=post_reach, reverse=True)[:limit]
    labels = [f"{(p.get('caption') or p['format'])[:40].replace(chr(10), ' ')} ({p['platform'][:2].upper()})" for p in ranked]
    values = [post_reach(p) for p in ranked]
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
            f"<td>{p['format']}</td><td>{post_reach(p):,}</td><td>{post_views(p):,}</td>"
            f"<td>{fmt(likes)}</td><td>{fmt(comments)}</td><td>{fmt(shares)}</td>"
            f"<td>{fmt(saves)}</td><td>{rate_str}</td>"
            f"<td><a href='{link}' target='_blank' rel='noopener'>{caption}</a></td></tr>"
        )

    return "".join(row(p) for p in ranked)


def render(account_snapshots, posts, crest_b64):
    generated_at = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    growth_dates, fb_growth, ig_growth = follower_growth_series(account_snapshots)
    top_labels, top_values = top_posts_for_chart(posts)
    has_growth_history = len(growth_dates) > 1
    has_top_posts = len(top_labels) > 0
    tracking_since = growth_dates[0] if growth_dates else generated_at

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Leamington RFC — Social Dashboard</title>
<link rel="stylesheet" href="https://api.fontshare.com/v2/css?f[]=supreme@400,700&display=swap">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root {{
    color-scheme: light dark;
    padding-top: env(safe-area-inset-top, 0px);
    padding-bottom: env(safe-area-inset-bottom, 0px);
    --blue: {BRAND['blue']}; --yellow: {BRAND['yellow']}; --red: {BRAND['red']}; --green: {BRAND['green']};
    --bg: #ffffff; --card-bg: #f6f5fb; --text: #1a1a1a; --muted: #666;
  }}
  :root[data-theme="dark"], :root:not([data-theme="light"]) {{ }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{ --bg: #14131c; --card-bg: #211f2e; --text: #f0f0f0; --muted: #aaa; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: 'Supreme', system-ui, sans-serif; font-weight: 300;
    max-width: 1080px; margin: 0 auto; padding: 2rem 1.2rem 4rem;
    background: var(--bg); color: var(--text);
  }}
  h1, h2, h3 {{ font-weight: 700; text-transform: uppercase; letter-spacing: 0.02em; }}
  header {{ display: flex; align-items: center; gap: 1.2rem; margin-bottom: 0.5rem; }}
  header img {{ height: 72px; width: auto; }}
  h1 {{ font-size: 1.7rem; color: var(--blue); margin: 0; }}
  .tagline {{ color: var(--muted); font-weight: 400; text-transform: none; font-size: 0.95rem; margin: 0.1rem 0 0; }}
  .updated {{ color: var(--muted); font-size: 0.85rem; margin: 0.3rem 0 2rem; }}
  h2 {{ font-size: 1.05rem; color: var(--blue); margin-top: 2.5rem; border-bottom: 3px solid var(--yellow); padding-bottom: 0.4rem; display: inline-block; }}
  .muted {{ color: var(--muted); font-weight: 400; }}

  .hero-row {{ display: flex; flex-wrap: wrap; gap: 1rem; margin-top: 1rem; }}
  .hero-card {{ flex: 1 1 260px; background: var(--card-bg); border-radius: 10px; border-top: 5px solid; padding: 1.2rem 1.4rem; }}
  .hero-card h3 {{ font-size: 0.85rem; margin: 0 0 0.4rem; color: var(--muted); }}
  .hero-number {{ font-size: 2.4rem; font-weight: 700; line-height: 1; }}
  .hero-label {{ font-size: 0.8rem; color: var(--muted); margin-top: 0.2rem; }}
  .hero-sub {{ font-size: 0.8rem; color: var(--muted); margin-top: 0.3rem; }}
  .delta {{ font-weight: 700; padding: 0.1rem 0.4rem; border-radius: 4px; font-size: 0.75rem; }}
  .delta.up {{ color: var(--green); }}
  .delta.down {{ color: var(--red); }}
  .delta.flat {{ color: var(--muted); }}
  .story {{ font-size: 0.85rem; margin-top: 0.7rem; padding-top: 0.6rem; border-top: 1px solid rgba(128,128,128,0.25); }}
  .story-pct {{ color: var(--green); }}

  .chart-wrap {{ position: relative; height: 320px; margin: 1rem 0 2rem; background: var(--card-bg); border-radius: 10px; padding: 1rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  th {{ background: var(--blue); color: white; text-align: left; padding: 0.5rem; font-weight: 700; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.03em; }}
  td {{ text-align: left; padding: 0.45rem 0.5rem; border-bottom: 1px solid rgba(128,128,128,0.2); white-space: nowrap; }}
  tbody tr:nth-child(even) {{ background: var(--card-bg); }}
  .table-wrap {{ overflow-x: auto; border-radius: 10px; }}
  a {{ color: var(--blue); }}
  footer {{ margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid rgba(128,128,128,0.25); display: flex; align-items: center; gap: 0.8rem; }}
  footer img {{ height: 36px; }}
  footer span {{ color: var(--blue); font-weight: 700; font-size: 0.85rem; letter-spacing: 0.03em; }}
</style>
</head>
<body>
  <header>
    <img src="data:image/png;base64,{crest_b64}" alt="Leamington RFC centenary crest">
    <div>
      <h1>Leamington RFC</h1>
      <p class="tagline">Social Media Dashboard</p>
    </div>
  </header>
  <p class="updated">Last updated {generated_at} · tracking daily since {tracking_since}</p>

  <div class="hero-row">
    {platform_hero(account_snapshots, 'facebook', 'Facebook', BRAND['blue'])}
    {platform_hero(account_snapshots, 'instagram', 'Instagram', BRAND['red'])}
  </div>

  <h2>Follower growth</h2>
  {"<div class='chart-wrap'><canvas id='growthChart'></canvas></div>" if has_growth_history else "<p class='muted'>Not enough daily history yet — this fills in day by day as the pipeline keeps running. The \"since\" figures above use dates you gave directly, separate from this chart.</p>"}

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

  <footer>
    <img src="data:image/png;base64,{crest_b64}" alt="">
    <span>ONE CLUB, ONE LEAM</span>
  </footer>

<script>
{"const growthCtx = document.getElementById('growthChart');" if has_growth_history else ""}
{f'''new Chart(growthCtx, {{
  type: 'line',
  data: {{
    labels: {json.dumps(growth_dates)},
    datasets: [
      {{ label: 'Facebook fans', data: {json.dumps(fb_growth)}, borderColor: '{BRAND["blue"]}', backgroundColor: '{BRAND["blue"]}22', tension: 0.25, spanGaps: true, fill: true }},
      {{ label: 'Instagram followers', data: {json.dumps(ig_growth)}, borderColor: '{BRAND["red"]}', backgroundColor: '{BRAND["red"]}22', tension: 0.25, spanGaps: true, fill: true }}
    ]
  }},
  options: {{ responsive: true, maintainAspectRatio: false, scales: {{ y: {{ beginAtZero: false }} }} }}
}});''' if has_growth_history else ""}

{"const topCtx = document.getElementById('topPostsChart');" if has_top_posts else ""}
{f'''new Chart(topCtx, {{
  type: 'bar',
  data: {{
    labels: {json.dumps(top_labels)},
    datasets: [{{ label: 'Reach', data: {json.dumps(top_values)}, backgroundColor: '{BRAND["yellow"]}', borderColor: '{BRAND["blue"]}', borderWidth: 1 }}]
  }},
  options: {{ indexAxis: 'y', responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
}});''' if has_top_posts else ""}
</script>
</body>
</html>"""


def main():
    account_snapshots = load_json(ACCOUNT_FILE)
    posts = load_json(POSTS_FILE)
    crest_b64 = CREST_B64_FILE.read_text(encoding="ascii").strip()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(render(account_snapshots, posts, crest_b64), encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
