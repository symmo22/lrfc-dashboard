#!/usr/bin/env python3
"""
Regenerates docs/index.html from the files in data/.

Pure local file processing, no network calls apart from the browser
loading Chart.js and the Supreme font when someone opens the page.

Reads:
  data/account_snapshots.json  daily follower counts (ingest.py)
  data/posts.json              per-post metrics (ingest_posts.py)
  data/insights.json           30-day totals and audience (ingest_insights.py)
Any of these may be missing or partial; every panel degrades to a plain
note rather than breaking the page.

Honesty rules this file follows:
  - Reach is never added across platforms (the same person can follow
    both). Views are counts, so they can be.
  - Posts are always shown per platform, never as one blended number.
  - HISTORICAL_ANCHORS are figures Mark gave directly, shown as a
    "then vs now" statement, never plotted as if measured daily.
  - Auto-written insights only appear when the data behind them meets a
    minimum sample size.
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_posts import LOOKBACK_DAYS  # noqa: E402 - single source of truth

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA_DIR = ROOT / "data"
ACCOUNT_FILE = DATA_DIR / "account_snapshots.json"
POSTS_FILE = DATA_DIR / "posts.json"
INSIGHTS_FILE = DATA_DIR / "insights.json"
OUTPUT_FILE = ROOT / "docs" / "index.html"
CREST_BLUE_FILE = HERE / "crest_b64.txt"

PERIOD_DAYS = 30
MIN_SAMPLE = 3          # smallest group an auto-insight will talk about
QUIET_MIN_AGE_DAYS = 7  # posts younger than this aren't called "quiet" yet

# Given by Mark directly, not fetched. Instagram exact, Facebook rounded.
HISTORICAL_ANCHORS = {
    "facebook": {"date": "2025-08-01", "count": 1800, "approx": True},
    "instagram": {"date": "2025-08-01", "count": 1043, "approx": False},
}

BRAND = {"blue": "#3E3787", "yellow": "#FAE226", "red": "#CF3B41", "green": "#60AC3F"}
PALETTE = [BRAND["blue"], BRAND["red"], BRAND["yellow"], BRAND["green"],
           "#8A84C4", "#E8898D", "#A9D48F", "#E9DB6A", "#26225A", "#9C3035", "#4F7F3A"]
GREY = "#A8A8B3"
PLATFORM_COLOUR = {"instagram": BRAND["red"], "facebook": BRAND["blue"]}
PLATFORM_NAME = {"instagram": "Instagram", "facebook": "Facebook"}

# ---------------------------------------------------------------------------
# Classification (captions only, no manual tagging, no Notion at run time)
# ---------------------------------------------------------------------------

# Labels match the real Notion Socials Planner taxonomy, checked 2026-09-21.
ALL_SECTIONS = ["Men's 1st XV", "Men's Lions (2nd XV)", "Colts (U18)", "Women's",
                "Juniors - Boys", "Juniors - Girls", "Minis", "Mixed Ability",
                "Touch Rugby", "Walking Rugby", "Whole Club"]

# Matching order: more specific before more general.
SECTION_KEYWORDS = [
    ("Men's Lions (2nd XV)", ["2nd xv", "lions"]),
    ("Men's 1st XV", ["1st xv", "first xv", "1sts"]),
    ("Colts (U18)", ["colts"]),
    ("Women's", ["women's", "womens", "ladies"]),
    ("Mixed Ability", ["mixed ability"]),
    ("Juniors - Girls", ["juniors girls", "junior girls", "girls u1", "u12 girls", "u14 girls", "u16 girls"]),
    ("Juniors - Boys", ["juniors boys", "junior boys", "boys u1", "u12 boys", "u14 boys", "u16 boys"]),
    ("Minis", ["minis"]),
    ("Walking Rugby", ["walking rugby"]),
    ("Touch Rugby", ["touch rugby", "tag rugby"]),
]

HISTORICAL_KEYWORDS = ["tbt", "throwback", "on this day", "founded", "history",
                       "centenary", "100 years", "anniversary"]
HISTORICAL_YEAR_PATTERN = re.compile(r"\b(19\d{2}|20[01]\d)\b")

# Shared by both classifiers so they can't drift apart.
COMMUNITY_VALUES_KEYWORDS = ["one club", "one leam", "whole club", "community",
                             "inclusion", "inclusive", "mental health"]

THEMES = ["Match Content", "Club Life", "Community & Values", "Heritage"]
CATEGORY_KEYWORDS = [
    ("Match Content", ["match report", "final score", "kick off", "kick-off", "fixtures",
                       "full time", "ft:", "preview", " v ", "final whistle"]),
    ("Club Life", ["training", "coach", "volunteer", "committee", "pitch", "clubhouse",
                   "groundwork", "behind the scenes", "sponsor", "fundraiser", "tickets",
                   "join us", "sign up", "sign-up", "taster", "registration", "open day",
                   "poll", "quiz", "caption this", "guess", "vote for"]),
    ("Community & Values", COMMUNITY_VALUES_KEYWORDS),
]


def looks_historical(text):
    if any(kw in text for kw in HISTORICAL_KEYWORDS):
        return True
    return bool(HISTORICAL_YEAR_PATTERN.search(text))


def classify_section(caption):
    text = (caption or "").lower()
    for section, keywords in SECTION_KEYWORDS:
        if any(kw in text for kw in keywords):
            return section
    if any(kw in text for kw in COMMUNITY_VALUES_KEYWORDS) or looks_historical(text):
        return "Whole Club"
    return "Unclassified"


def classify_category(caption):
    text = (caption or "").lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(kw in text for kw in keywords):
            return category
    if looks_historical(text):
        return "Heritage"
    return "Unclassified"


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_json(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def read_text(path):
    return path.read_text(encoding="ascii").strip() if path.exists() else ""


def published_date(p):
    return datetime.fromisoformat(p["published_at"].replace("Z", "+00:00")).date()


def latest_post_metrics(p):
    snaps = p.get("snapshots") or {}
    return snaps[max(snaps)] if snaps else {}


def post_reach(p):
    m = latest_post_metrics(p)
    return m.get("reach") or m.get("post_total_media_view_unique") or 0


def post_views(p):
    m = latest_post_metrics(p)
    return m.get("views") or m.get("post_media_view") or 0


def post_counts(p):
    static = p.get("static_metrics") or {}
    m = latest_post_metrics(p)
    if p["platform"] == "instagram":
        return static.get("likes"), static.get("comments"), m.get("shares"), m.get("saved")
    return static.get("reactions"), static.get("comments"), static.get("shares"), None


def post_interactions(p):
    return sum(v or 0 for v in post_counts(p))


def post_engagement_rate(p):
    reach = post_reach(p)
    if not reach:
        return None
    return round(100 * post_interactions(p) / reach, 1)


def latest_by_platform(snapshots):
    latest = {}
    for r in snapshots.values():
        if r["platform"] not in latest or r["date"] > latest[r["platform"]]["date"]:
            latest[r["platform"]] = r
    return latest


def previous_snapshot(snapshots, platform, before):
    older = [r for r in snapshots.values() if r["platform"] == platform and r["date"] < before]
    return max(older, key=lambda r: r["date"]) if older else None


def follower_count(record, platform):
    if not record:
        return None
    m = record.get("metrics") or {}
    return m.get("fan_count") if platform == "facebook" else m.get("followers_count")


def group_stats(posts_list, key_fn):
    groups = {}
    for p in posts_list:
        groups.setdefault(key_fn(p), []).append(p)
    rows = []
    for key, ps in groups.items():
        reaches = [post_reach(p) for p in ps]
        rows.append({
            "key": key,
            "count": len(ps),
            "avg_reach": round(sum(reaches) / len(reaches)) if reaches else 0,
            "views": sum(post_views(p) for p in ps),
        })
    return sorted(rows, key=lambda r: r["count"], reverse=True)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def num(v):
    return "—" if v is None else f"{v:,}"


def pct_change(cur, prev):
    if cur is None or not prev:
        return None
    return round(100 * (cur - prev) / prev)


def delta_html(cur, prev, suffix):
    change = pct_change(cur, prev)
    if change is None:
        return ""
    if change == 0:
        return f"<span class='delta flat'>no change {suffix}</span>"
    cls, arrow = ("up", "▲") if change > 0 else ("down", "▼")
    return f"<span class='delta {cls}'>{arrow} {abs(change)}% {suffix}</span>"


def join_names(names):
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def stable_colours(labels, reference):
    out = []
    for i, label in enumerate(labels):
        if label == "Unclassified":
            out.append(GREY)
        elif label in reference:
            out.append(PALETTE[reference.index(label) % len(PALETTE)])
        else:
            out.append(PALETTE[(len(reference) + i) % len(PALETTE)])
    return out


FORMAT_LABELS = {"image": "Image", "carousel": "Carousel", "reel": "Reel", "video": "Video",
                 "text_or_link": "Text or link", "unknown": "Other"}
FORMAT_ORDER = ["Reel", "Video", "Carousel", "Image", "Text or link", "Other"]
PRODUCT_LABELS = {"POST": "Posts", "FEED": "Posts", "REEL": "Reels", "STORY": "Stories",
                  "CAROUSEL_CONTAINER": "Carousels", "AD": "Ads", "IGTV": "Video"}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
AGE_ORDER = ["13-17", "18-24", "25-34", "35-44", "45-54", "55-64", "65+"]
GENDER_LABELS = {"F": "Women", "M": "Men", "U": "Not specified"}


def format_label(p):
    return FORMAT_LABELS.get(p.get("format"), (p.get("format") or "Other").title())


# ---------------------------------------------------------------------------
# Chart configs (plain JSON, drawn by Chart.js in the browser)
# ---------------------------------------------------------------------------

def donut(cid, labels, values, colours, centre="", sub=""):
    return {"id": cid, "config": {
        "type": "doughnut",
        "data": {"labels": labels, "datasets": [{"data": values, "backgroundColor": colours, "borderWidth": 2}]},
        "options": {"responsive": True, "maintainAspectRatio": False, "cutout": "64%",
                    "plugins": {"legend": {"position": "bottom", "labels": {"boxWidth": 12, "padding": 10}},
                                "centerText": {"text": centre, "sub": sub}}},
    }}


def bars(cid, labels, values, colours, horizontal=False, label=""):
    value_axis, category_axis = ("x", "y") if horizontal else ("y", "x")
    return {"id": cid, "config": {
        "type": "bar",
        "data": {"labels": labels, "datasets": [{"label": label, "data": values, "backgroundColor": colours,
                                                 "borderRadius": 6, "maxBarThickness": 44}]},
        "options": {"indexAxis": "y" if horizontal else "x", "responsive": True, "maintainAspectRatio": False,
                    "plugins": {"legend": {"display": False}},
                    "scales": {category_axis: {"grid": {"display": False}},
                               value_axis: {"beginAtZero": True}}},
    }}


def line(cid, labels, datasets):
    return {"id": cid, "config": {
        "type": "line",
        "data": {"labels": labels, "datasets": datasets},
        "options": {"responsive": True, "maintainAspectRatio": False,
                    "plugins": {"legend": {"position": "bottom"}},
                    "scales": {"y": {"beginAtZero": False}, "x": {"grid": {"display": False}}}},
    }}


# ---------------------------------------------------------------------------
# HTML building blocks
# ---------------------------------------------------------------------------

def stat_card(title, big, sub="", delta="", accent=None):
    style = f" style='border-top-color:{accent}'" if accent else ""
    sub_html = f"<div class='sub'>{sub}</div>" if sub else ""
    return f"<div class='stat'{style}><h3>{title}</h3><div class='big'>{big}</div>{sub_html}{delta}</div>"


def chart_card(title, note, cid, aria, tall=False):
    cls = "chart tall" if tall else "chart"
    note_html = f"<p class='note'>{note}</p>" if note else ""
    return (f"<div class='card'><h3>{title}</h3>{note_html}"
            f"<div class='{cls}'><canvas id='{cid}' role='img' aria-label='{escape(aria, quote=True)}'></canvas></div></div>")


def empty_card(title, message):
    return f"<div class='card'><h3>{title}</h3><p class='note'>{message}</p></div>"


def plural(n, word):
    if n is None:
        return f"<b>—</b> {word}s"
    return f"<b>{n:,}</b> {word}" + ("" if n == 1 else "s")


def post_card(p, rank=None):
    platform = p["platform"]
    image = p.get("image")
    img_html = ""
    if image:
        img_html = (f"<img src='{escape(image, quote=True)}' alt='' loading='lazy' "
                    "onerror=\"this.closest('.thumb').classList.add('noimg');this.remove()\">")
    thumb_cls = "thumb" if image else "thumb noimg"
    rank_html = f"<span class='rank'>{rank}</span>" if rank else ""
    caption = escape((p.get("caption") or "").strip() or "(no caption)")
    likes, comments, shares, _ = post_counts(p)
    date = published_date(p).strftime("%a %d %b")
    link = escape(p.get("permalink") or "#", quote=True)
    return f"""<a class='post' href='{link}' target='_blank' rel='noopener'>
  <div class='{thumb_cls}'>{img_html}<span class='badge' style='background:{PLATFORM_COLOUR[platform]}'>{PLATFORM_NAME[platform]}</span>{rank_html}</div>
  <div class='body'>
    <div class='date'>{date} · {escape(format_label(p))}</div>
    <div class='cap'>{caption}</div>
    <div class='nums'><span><b>{num(post_reach(p))}</b> reach</span><span><b>{num(post_views(p))}</b> views</span>
    <span>{plural(likes, 'like')}</span><span>{plural(comments, 'comment')}</span><span>{plural(shares, 'share')}</span></div>
  </div>
</a>"""


def platform_growth_card(snapshots, platform):
    colour = PLATFORM_COLOUR[platform]
    name = PLATFORM_NAME[platform]
    latest = latest_by_platform(snapshots).get(platform)
    if not latest:
        return stat_card(f"{name} followers", "—", "No data yet", accent=colour)
    cur = follower_count(latest, platform)
    prev = follower_count(previous_snapshot(snapshots, platform, latest["date"]), platform)
    delta = ""
    if cur is not None and prev is not None:
        diff = cur - prev
        if diff == 0:
            delta = "<span class='delta flat'>no change since yesterday</span>"
        else:
            cls, arrow = ("up", "▲") if diff > 0 else ("down", "▼")
            delta = f"<span class='delta {cls}'>{arrow} {abs(diff)} since yesterday</span>"
    story = ""
    anchor = HISTORICAL_ANCHORS.get(platform)
    if anchor and cur:
        growth = round(100 * (cur - anchor["count"]) / anchor["count"])
        approx = "~" if anchor.get("approx") else ""
        when = datetime.fromisoformat(anchor["date"]).strftime("%B %Y")
        sign = "+" if growth >= 0 else ""
        story = (f"<div class='story'>{approx}{anchor['count']:,} → {cur:,} since {when}"
                 f" <strong>{sign}{growth}%</strong></div>")
    return f"<div class='stat' style='border-top-color:{colour}'><h3>{name} followers</h3><div class='big'>{num(cur)}</div>{delta}{story}</div>"


# ---------------------------------------------------------------------------
# Auto-written insights
# ---------------------------------------------------------------------------

def insight_lines(insights, cur_posts, section_rows, theme_rows, format_rows, day_rows):
    lines = []

    for platform in ("instagram", "facebook"):
        block = insights.get(platform) or {}
        cur = (block.get("current") or {}).get("views")
        prev = (block.get("prior") or {}).get("views")
        change = pct_change(cur, prev)
        if change is not None and abs(change) >= 5:
            direction = "up" if change > 0 else "down"
            lines.append(f"<strong>{PLATFORM_NAME[platform]} views are {direction} {abs(change)}%</strong> "
                         f"on the previous 30 days ({cur:,} against {prev:,}).")

    split = (insights.get("instagram") or {}).get("views_by_follower_type") or {}
    nf, f = split.get("NON_FOLLOWER"), split.get("FOLLOWER")
    if nf is not None and f is not None and nf + f > 0:
        share = round(100 * nf / (nf + f))
        lines.append(f"<strong>{share}% of Instagram views came from people who don't follow the club.</strong> "
                     "That's where new players, parents and supporters come from.")

    def compare(rows, suffix):
        usable = [r for r in rows if r["count"] >= MIN_SAMPLE and r["key"] != "Unclassified" and r["avg_reach"] > 0]
        if len(usable) < 2:
            return
        best = max(usable, key=lambda r: r["avg_reach"])
        worst = min(usable, key=lambda r: r["avg_reach"])
        ratio = best["avg_reach"] / worst["avg_reach"]
        if ratio >= 1.3:
            lines.append(f"<strong>{escape(best['key'])} {suffix} averaged {best['avg_reach']:,} reach</strong>, "
                         f"{ratio:.1f}× {escape(worst['key'])} {suffix} ({worst['avg_reach']:,}).")

    compare(format_rows, "posts")
    compare(theme_rows, "posts")

    usable_days = [r for r in day_rows if r["count"] >= MIN_SAMPLE]
    if len(usable_days) >= 2:
        best = max(usable_days, key=lambda r: r["avg_reach"])
        lines.append(f"<strong>{best['key']} posts reach the most people</strong>, averaging "
                     f"{best['avg_reach']:,} across {best['count']} posts.")

    if cur_posts:
        present = {r["key"] for r in section_rows}
        missing = [s for s in ALL_SECTIONS if s != "Whole Club" and s not in present]
        if missing:
            lines.append(f"<strong>No captions in the last 30 days named {escape(join_names(missing))}.</strong> "
                         "Worth a look for One Club, One Leam balance.")
        unclassified = next((r for r in section_rows if r["key"] == "Unclassified"), None)
        if unclassified and unclassified["count"] / len(cur_posts) >= 0.25:
            share = round(100 * unclassified["count"] / len(cur_posts))
            lines.append(f"{share}% of posts didn't name a team or section in the caption, so the section view "
                         "undercounts. Naming the team in match captions fixes it.")
    return lines


# ---------------------------------------------------------------------------
# Page sections
# ---------------------------------------------------------------------------

def overview_section(insights, cur_posts, prev_posts, lines, charts):
    ig = insights.get("instagram") or {}
    fb = insights.get("facebook") or {}
    igc, igp = ig.get("current") or {}, ig.get("prior") or {}
    fbc, fbp = fb.get("current") or {}, fb.get("prior") or {}

    ig_views, fb_views = igc.get("views"), fbc.get("views")
    present = [(v, p.get("views")) for v, p in ((ig_views, igp), (fb_views, fbp)) if v is not None]
    total_views = sum(v for v, _ in present) if present else None
    total_prev = sum(p for _, p in present) if present and all(p is not None for _, p in present) else None

    ig_count = sum(1 for p in cur_posts if p["platform"] == "instagram")
    fb_count = sum(1 for p in cur_posts if p["platform"] == "facebook")
    cur_int = sum(post_interactions(p) for p in cur_posts)
    prev_int = sum(post_interactions(p) for p in prev_posts)

    cards = [
        stat_card("Total views", num(total_views),
                  f"Instagram {num(ig_views)} · Facebook {num(fb_views)}",
                  delta_html(total_views, total_prev, "vs previous 30 days"), BRAND["yellow"]),
        stat_card("Instagram reach", num(igc.get("reach")), "accounts that saw your content",
                  delta_html(igc.get("reach"), igp.get("reach"), "vs previous 30 days"), BRAND["red"]),
        stat_card("Facebook reach", num(fbc.get("reach_28d")), "people who saw your content, 28 days",
                  delta_html(fbc.get("reach_28d"), fbp.get("reach_28d"), "vs previous 28 days"), BRAND["blue"]),
        stat_card("Likes, comments & shares", num(cur_int if cur_posts else None),
                  "on posts published in the last 30 days",
                  delta_html(cur_int, prev_int, "vs previous 30 days")
                  if cur_posts and len(prev_posts) >= MIN_SAMPLE else "", BRAND["green"]),
        stat_card("Posts published",
                  f"<span class='split'><span>{ig_count}<small>Instagram</small></span>"
                  f"<span>{fb_count}<small>Facebook</small></span></span>",
                  "last 30 days", "", BRAND["yellow"]),
    ]

    follows = ig.get("follows") or {}
    if "FOLLOWER" in follows and "NON_FOLLOWER" in follows:
        gained, lost = follows["FOLLOWER"], follows["NON_FOLLOWER"]
        net = gained - lost
        cards.append(stat_card("Instagram net followers", f"{'+' if net >= 0 else ''}{net:,}",
                               f"{gained:,} followed · {lost:,} unfollowed", "", BRAND["green"]))

    if ig_views is not None and fb_views is not None and ig_views + fb_views > 0:
        charts.append(donut("viewsSplit", ["Instagram", "Facebook"], [ig_views, fb_views],
                            [BRAND["red"], BRAND["blue"]], f"{total_views:,}", "views, 30 days"))
        split_html = chart_card("Where views came from", "Last 30 days, both platforms", "viewsSplit",
                                "Donut chart of views by platform")
    else:
        split_html = empty_card("Where views came from", "Waiting for 30-day totals from both platforms.")

    insight_html = ("<ul class='insights'>" + "".join(f"<li>{l}</li>" for l in lines) + "</ul>") if lines else \
        "<p class='note'>Not enough data yet for reliable observations. These fill in as posts build up.</p>"

    return f"""<section id='overview'>
  <h2>The last 30 days</h2>
  <p class='lede'>Headline numbers against the 30 days before. Reach is shown per platform because the same person can follow both; views can be added together.</p>
  <div class='stats'>{''.join(cards)}</div>
  <div class='grid-insights'>
    <div class='card'><h3>What the numbers say</h3><p class='note'>Written automatically from the data on each run.</p>{insight_html}</div>
    {split_html}
  </div>
</section>"""


def audience_section(snapshots, insights, charts):
    cards = platform_growth_card(snapshots, "instagram") + platform_growth_card(snapshots, "facebook")

    by_platform = {"facebook": {}, "instagram": {}}
    for r in snapshots.values():
        c = follower_count(r, r["platform"])
        if c is not None:
            by_platform[r["platform"]][r["date"]] = c
    dates = sorted({d for m in by_platform.values() for d in m})
    if len(dates) > 1:
        chart = line("growthChart", dates, [
            {"label": "Instagram (left scale)", "data": [by_platform["instagram"].get(d) for d in dates],
             "borderColor": BRAND["red"], "backgroundColor": BRAND["red"], "cubicInterpolationMode": "monotone",
             "spanGaps": True, "yAxisID": "yIG", "pointRadius": 3},
            {"label": "Facebook (right scale)", "data": [by_platform["facebook"].get(d) for d in dates],
             "borderColor": BRAND["blue"], "backgroundColor": BRAND["blue"], "cubicInterpolationMode": "monotone",
             "spanGaps": True, "yAxisID": "yFB", "pointRadius": 3},
        ])
        # Separate scales: on one shared axis both lines sit flat because the
        # two accounts are ~700 followers apart, hiding day-to-day change.
        chart["config"]["options"]["scales"] = {
            "x": {"grid": {"display": False}},
            "yIG": {"position": "left", "ticks": {"color": BRAND["red"], "precision": 0}},
            "yFB": {"position": "right", "ticks": {"color": BRAND["blue"], "precision": 0},
                    "grid": {"drawOnChartArea": False}},
        }
        charts.append(chart)
        growth = chart_card("Followers, day by day",
                            f"Tracked daily since {dates[0]}. Each platform has its own scale so small changes show.",
                            "growthChart",
                            "Line chart of follower counts over time", tall=True)
    else:
        growth = empty_card("Followers, day by day",
                            "Fills in day by day now the pipeline is running. The growth since August 2025 "
                            "above uses the figures you gave directly.")

    ig = insights.get("instagram") or {}
    watch_cards = []

    split = ig.get("views_by_follower_type") or {}
    nf, f = split.get("NON_FOLLOWER"), split.get("FOLLOWER")
    if nf is not None and f is not None and nf + f > 0:
        share = round(100 * nf / (nf + f))
        charts.append(donut("followerSplit", ["Followers", "Not following yet"], [f, nf],
                            [BRAND["blue"], BRAND["yellow"]], f"{share}%", "non-followers"))
        watch_cards.append(chart_card("Who's watching on Instagram",
                                      "Views from followers vs people who don't follow yet, 30 days",
                                      "followerSplit", "Donut chart of followers vs non-followers"))
    else:
        watch_cards.append(empty_card("Who's watching on Instagram", "Not returned by Meta on the last run."))

    product = ig.get("views_by_product_type") or {}
    product = {PRODUCT_LABELS.get(k, k.replace("_", " ").title()): v for k, v in product.items() if v}
    if product:
        labels = sorted(product, key=lambda k: product[k], reverse=True)
        values = [product[k] for k in labels]
        charts.append(donut("productSplit", labels, values, PALETTE[:len(labels)],
                            f"{sum(values):,}", "Instagram views"))
        watch_cards.append(chart_card("What they watched",
                                      "Instagram views by content type, 30 days, Stories included",
                                      "productSplit", "Donut chart of views by content type"))
    else:
        watch_cards.append(empty_card("What they watched", "Not returned by Meta on the last run."))

    demo = ig.get("demographics") or {}
    demo_cards = []
    age = (demo.get("age") or {}).get("values") or {}
    if age:
        labels = [a for a in AGE_ORDER if a in age] + [a for a in age if a not in AGE_ORDER]
        charts.append(bars("ageChart", labels, [age[a] for a in labels], BRAND["blue"], label="Followers"))
        demo_cards.append(chart_card("Age", "Instagram followers", "ageChart", "Bar chart of followers by age"))
    gender = (demo.get("gender") or {}).get("values") or {}
    if gender:
        labels = [GENDER_LABELS.get(k, k) for k in gender]
        values = list(gender.values())
        colours = [{"Women": BRAND["red"], "Men": BRAND["blue"]}.get(l, GREY) for l in labels]
        top_label = labels[values.index(max(values))]
        charts.append(donut("genderChart", labels, values, colours,
                            f"{round(100 * max(values) / sum(values))}%", top_label.lower()))
        demo_cards.append(chart_card("Gender", "Instagram followers", "genderChart",
                                     "Donut chart of followers by gender"))
    city = (demo.get("city") or {}).get("values") or {}
    if city:
        top = sorted(city.items(), key=lambda kv: kv[1], reverse=True)[:8]
        charts.append(bars("cityChart", [k.split(",")[0] for k, _ in top], [v for _, v in top],
                           BRAND["red"], horizontal=True, label="Followers"))
        demo_cards.append(chart_card("Top towns and cities", "Instagram followers", "cityChart",
                                     "Bar chart of followers by city"))
    if not demo_cards:
        demo_cards.append(empty_card("Audience",
                                     "Meta didn't return follower demographics on the last run. "
                                     "Facebook no longer shares age or gender at all."))

    return f"""<section id='audience'>
  <h2>Audience</h2>
  <p class='lede'>How the following has grown, who is watching, and who they are.</p>
  <div class='stats'>{cards}</div>
  <div class='grid1'>{growth}</div>
  <div class='grid2'>{''.join(watch_cards)}</div>
  <div class='grid3'>{''.join(demo_cards)}</div>
</section>"""


def content_section(cur_posts, format_rows, day_rows, charts):
    if not cur_posts:
        return """<section id='content'><h2>What works</h2><p class='note'>No posts in the last 30 days yet.</p></section>"""

    rows = sorted(format_rows, key=lambda r: FORMAT_ORDER.index(r["key"]) if r["key"] in FORMAT_ORDER else 99)
    labels = [r["key"] for r in rows]
    colours = stable_colours(labels, FORMAT_ORDER)
    total_views = sum(r["views"] for r in rows)
    if total_views:
        charts.append(donut("formatViews", labels, [r["views"] for r in rows], colours,
                            f"{total_views:,}", "views on these posts"))
        fmt_share = chart_card("Views by format", "Posts published in the last 30 days, both platforms",
                               "formatViews", "Donut chart of views by format")
    else:
        fmt_share = empty_card("Views by format", "No view figures on these posts yet.")

    ranked = sorted(rows, key=lambda r: r["avg_reach"], reverse=True)
    charts.append(bars("formatReach", [f"{r['key']} ({r['count']})" for r in ranked],
                       [r["avg_reach"] for r in ranked], stable_colours([r["key"] for r in ranked], FORMAT_ORDER),
                       horizontal=True, label="Average reach"))
    fmt_perf = chart_card("Average reach per post, by format", "Number of posts in brackets",
                          "formatReach", "Bar chart of average reach by format")

    by_day = {r["key"]: r for r in day_rows}
    days = [d for d in DAYS if d in by_day]
    solid = [d for d in days if by_day[d]["count"] >= MIN_SAMPLE]
    best = max((by_day[d]["avg_reach"] for d in solid), default=None)
    charts.append(bars("dayReach", [f"{d[:3]} ({by_day[d]['count']})" for d in days],
                       [by_day[d]["avg_reach"] for d in days],
                       [BRAND["yellow"] if best is not None and by_day[d]["avg_reach"] == best and by_day[d]["count"] >= MIN_SAMPLE
                        else BRAND["blue"] for d in days],
                       label="Average reach"))
    day_perf = chart_card("Best day to post", "Average reach by day published, UK time. Posts in brackets. Yellow marks the best day with 3+ posts.",
                          "dayReach", "Bar chart of average reach by weekday")

    return f"""<section id='content'>
  <h2>What works</h2>
  <p class='lede'>Which formats and days earn the most attention, from posts published in the last 30 days.</p>
  <div class='grid2'>{fmt_share}{fmt_perf}</div>
  <div class='grid1'>{day_perf}</div>
</section>"""


def club_section(cur_posts, section_rows, theme_rows, charts):
    counts = {r["key"]: r["count"] for r in section_rows}
    chips = "".join(
        f"<span class='chip{'' if counts.get(s) else ' zero'}'>{escape(s)}<b>{counts.get(s, 0)}</b></span>"
        for s in ALL_SECTIONS
    )
    if counts.get("Unclassified"):
        chips += f"<span class='chip muted'>Not named in caption<b>{counts['Unclassified']}</b></span>"

    if not cur_posts:
        return f"""<section id='club'><h2>One Club, One Leam</h2><div class='chips'>{chips}</div>
<p class='note'>No posts in the last 30 days yet.</p></section>"""

    section_ref = ALL_SECTIONS + ["Unclassified"]
    theme_ref = THEMES + ["Unclassified"]

    def pair(rows, ref, prefix, title):
        labels = [r["key"] for r in rows]
        charts.append(donut(f"{prefix}Share", labels, [r["count"] for r in rows], stable_colours(labels, ref),
                            str(sum(r["count"] for r in rows)), "posts"))
        ranked = sorted(rows, key=lambda r: r["avg_reach"], reverse=True)
        charts.append(bars(f"{prefix}Reach", [f"{r['key']} ({r['count']})" for r in ranked],
                           [r["avg_reach"] for r in ranked], stable_colours([r["key"] for r in ranked], ref),
                           horizontal=True, label="Average reach"))
        return (chart_card(f"{title}: share of posts", "Last 30 days, both platforms", f"{prefix}Share",
                           f"Donut chart of posts by {title.lower()}")
                + chart_card(f"{title}: average reach", "Posts in brackets. Read little into groups under 3.",
                             f"{prefix}Reach", f"Bar chart of average reach by {title.lower()}", tall=True))

    return f"""<section id='club'>
  <h2>One Club, One Leam</h2>
  <p class='lede'>Every section of the club and how often it featured in the last 30 days. Red means no caption named it. Worked out from caption text, so naming the team in each caption makes this sharper.</p>
  <div class='chips'>{chips}</div>
  <div class='grid2'>{pair(section_rows, section_ref, 'section', 'Section')}</div>
  <div class='grid2'>{pair(theme_rows, theme_ref, 'theme', 'Theme')}</div>
</section>"""


def posts_section(cur_posts, all_tracked, today):
    top = sorted(cur_posts, key=post_reach, reverse=True)[:6]
    top_ids = {id(p) for p in top}
    quiet = sorted(
        [p for p in cur_posts if id(p) not in top_ids and (today - published_date(p)).days >= QUIET_MIN_AGE_DAYS],
        key=post_reach,
    )[:3]

    top_html = ("<div class='posts'>" + "".join(post_card(p, i + 1) for i, p in enumerate(top)) + "</div>") \
        if top else "<p class='note'>No posts in the last 30 days yet.</p>"
    quiet_html = ""
    if quiet:
        quiet_html = ("<h3 class='sub-h'>Quieter posts worth learning from</h3>"
                      "<p class='note'>Lowest reach among posts at least a week old.</p>"
                      "<div class='posts'>" + "".join(post_card(p) for p in quiet) + "</div>")

    rows = sorted(all_tracked, key=post_reach, reverse=True)[:80]
    body = ""
    for p in rows:
        likes, comments, shares, saves = post_counts(p)
        rate = post_engagement_rate(p)
        caption = escape((p.get("caption") or "")[:70] + ("…" if len(p.get("caption") or "") > 70 else ""))
        body += (f"<tr><td>{published_date(p).isoformat()}</td><td>{PLATFORM_NAME[p['platform']]}</td>"
                 f"<td>{escape(format_label(p))}</td><td>{escape(classify_section(p.get('caption')))}</td>"
                 f"<td>{escape(classify_category(p.get('caption')))}</td>"
                 f"<td>{num(post_reach(p))}</td><td>{num(post_views(p))}</td><td>{num(likes)}</td>"
                 f"<td>{num(comments)}</td><td>{num(shares)}</td><td>{num(saves)}</td>"
                 f"<td>{'—' if rate is None else str(rate) + '%'}</td>"
                 f"<td><a href='{escape(p.get('permalink') or '#', quote=True)}' target='_blank' rel='noopener'>{caption}</a></td></tr>")

    return f"""<section id='posts'>
  <h2>Posts</h2>
  <p class='lede'>The furthest-reaching posts of the last 30 days. Tap any card to open the post.</p>
  {top_html}
  {quiet_html}
  <details>
    <summary>Every tracked post, last {LOOKBACK_DAYS} days ({len(all_tracked)})</summary>
    <div class='table-wrap'><table>
      <thead><tr><th>Date</th><th>Platform</th><th>Format</th><th>Section</th><th>Theme</th><th>Reach</th><th>Views</th>
      <th>Likes</th><th>Comments</th><th>Shares</th><th>Saves</th><th>Eng. rate</th><th>Caption</th></tr></thead>
      <tbody>{body}</tbody>
    </table></div>
  </details>
</section>"""


def notes_section(insights, today):
    notes = [
        "Instagram figures use the last 30 days, matching the Instagram app. Facebook reach uses Meta's own 28-day window, because unique people can't be added up day by day.",
        "Reach is never added across platforms: the same person can follow both. Views can be, so the total views figure combines them.",
        "Posts shared into Facebook groups, and ads built directly in Ads Manager, don't appear on the Page and aren't included.",
        "Section and theme are read from caption text. A caption that doesn't name the team can't be placed.",
        "Follower growth since August 2025 uses figures supplied by the Communications Manager (Facebook rounded to 1,800). Daily tracking began when this dashboard went live.",
    ]
    generated = insights.get("generated_at")
    if not generated:
        notes.append("30-day totals and audience data haven't been collected yet. They appear after the next run.")
    else:
        age_days = (today - datetime.fromisoformat(generated).date()).days
        if age_days > 2:
            notes.append(f"30-day totals were last refreshed {age_days} days ago.")
    for n in insights.get("notes") or []:
        notes.append("Not available on the last run: " + escape(n.split(":")[0]) + ".")
    return "<section id='notes'><h2>About these numbers</h2><ul class='notes'>" + \
        "".join(f"<li>{n}</li>" for n in notes) + "</ul></section>"


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

CSS = """
:root{color-scheme:light dark;--blue:#3E3787;--yellow:#FAE226;--red:#CF3B41;--green:#60AC3F;
--bg:#ffffff;--card-bg:#f5f4fa;--text:#1a1a1a;--muted:#62606f;--heading:#3E3787;--line:rgba(128,128,128,.22)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#13121b;--card-bg:#201e2d;--text:#f1f0f6;--muted:#a9a6b8;--heading:#b7b1f2}}
:root[data-theme="dark"]{--bg:#13121b;--card-bg:#201e2d;--text:#f1f0f6;--muted:#a9a6b8;--heading:#b7b1f2}
*{box-sizing:border-box}
html{scroll-padding-top:64px;scroll-behavior:smooth}
body{margin:0;font-family:'Supreme',system-ui,-apple-system,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);line-height:1.4}
.banner{background:linear-gradient(135deg,#3E3787 0%,#26225A 100%);color:#fff;padding:2rem 1.2rem 2.2rem;border-bottom:6px solid var(--yellow)}
.banner-inner{max-width:1120px;margin:0 auto;display:flex;align-items:center;gap:1.4rem}
.crest-tile{background:#fff;border-radius:16px;padding:.55rem .6rem;flex-shrink:0;box-shadow:0 6px 18px rgba(0,0,0,.25)}
.crest-tile img{height:104px;width:auto;display:block}
.banner h1{margin:0;font-size:clamp(1.5rem,4vw,2.4rem);text-transform:uppercase;letter-spacing:.03em;line-height:1.05}
.banner .kicker{font-size:.8rem;text-transform:uppercase;letter-spacing:.12em;color:var(--yellow);font-weight:700}
.banner p{margin:.45rem 0 0;opacity:.85;font-size:.9rem}
nav{position:sticky;top:0;z-index:10;background:var(--bg);border-bottom:1px solid var(--line)}
nav .inner{max-width:1120px;margin:0 auto;display:flex;gap:.25rem;overflow-x:auto;padding:.5rem 1.2rem}
nav a{padding:.45rem .85rem;border-radius:999px;text-decoration:none;color:var(--text);font-weight:700;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}
nav a:hover{background:var(--card-bg)}
main{max-width:1120px;margin:0 auto;padding:1rem 1.2rem 3rem}
section{margin-top:2.6rem}
h2{font-size:1.15rem;text-transform:uppercase;letter-spacing:.04em;color:var(--heading);margin:0;border-bottom:3px solid var(--yellow);display:inline-block;padding-bottom:.3rem}
.lede{color:var(--muted);margin:.6rem 0 1.2rem;max-width:72ch;font-size:.93rem}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:1rem}
.stat{background:var(--card-bg);border-radius:14px;padding:1.1rem 1.2rem;border-top:5px solid var(--yellow)}
.stat h3,.card h3{margin:0 0 .45rem;font-size:.74rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.stat .big{font-size:2.1rem;font-weight:700;line-height:1.05}
.stat .sub{font-size:.8rem;color:var(--muted);margin-top:.35rem}
.split{display:flex;gap:1.2rem}
.split span{display:flex;flex-direction:column}
.split small{font-size:.7rem;font-weight:400;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.delta{display:inline-block;margin-top:.5rem;font-weight:700;font-size:.78rem}
.delta.up{color:var(--green)}.delta.down{color:var(--red)}.delta.flat{color:var(--muted)}
.story{font-size:.84rem;margin-top:.7rem;padding-top:.6rem;border-top:1px solid var(--line)}
.story strong{color:var(--green)}
.grid1,.grid2,.grid3,.grid-insights{display:grid;gap:1rem;margin-top:1rem}
.grid2{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.grid3{grid-template-columns:repeat(auto-fit,minmax(270px,1fr))}
.grid-insights{grid-template-columns:minmax(0,1.6fr) minmax(0,1fr)}
@media (max-width:760px){.grid-insights{grid-template-columns:1fr}}
.card{background:var(--card-bg);border-radius:14px;padding:1.1rem 1.2rem;min-width:0}
.note{font-size:.8rem;color:var(--muted);margin:0 0 .7rem}
.chart{position:relative;height:270px}
.chart.tall{height:330px}
.insights{list-style:none;margin:0;padding:0;display:grid;gap:.6rem}
.insights li{background:var(--bg);border-left:5px solid var(--heading);border-radius:8px;padding:.75rem .9rem;font-size:.92rem}
.chips{display:flex;flex-wrap:wrap;gap:.5rem;margin:.2rem 0 .4rem}
.chip{border-radius:999px;padding:.38rem .85rem;font-size:.8rem;font-weight:700;background:var(--card-bg);border:2px solid transparent}
.chip b{margin-left:.45rem;opacity:.75}
.chip.zero{border-color:var(--red);color:var(--red);background:transparent}
.chip.muted{color:var(--muted);font-style:italic}
.posts{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:1rem}
.post{background:var(--card-bg);border-radius:14px;overflow:hidden;display:flex;flex-direction:column;text-decoration:none;color:inherit;transition:transform .15s ease}
.post:hover{transform:translateY(-3px)}
.thumb{aspect-ratio:4/3;background:linear-gradient(135deg,#3E3787,#26225A);position:relative}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumb.noimg::after{content:"No preview";position:absolute;inset:0;display:grid;place-items:center;color:#fff;opacity:.6;font-size:.8rem}
.badge{position:absolute;top:.6rem;left:.6rem;color:#fff;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;padding:.25rem .55rem;border-radius:6px}
.rank{position:absolute;top:.55rem;right:.6rem;background:var(--yellow);color:#1a1a1a;font-weight:700;border-radius:999px;width:1.9rem;height:1.9rem;display:grid;place-items:center;font-size:.85rem}
.post .body{padding:.85rem 1rem 1rem;display:flex;flex-direction:column;gap:.5rem;flex:1}
.post .date{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.post .cap{font-size:.88rem;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.post .nums{display:flex;gap:.3rem .9rem;flex-wrap:wrap;font-size:.76rem;color:var(--muted);margin-top:auto}
.post .nums b{color:var(--text);font-size:.92rem}
.sub-h{margin:2rem 0 .3rem;font-size:.85rem;text-transform:uppercase;letter-spacing:.05em}
details{margin-top:1.5rem;background:var(--card-bg);border-radius:14px;padding:.9rem 1.1rem}
summary{cursor:pointer;font-weight:700}
.table-wrap{overflow-x:auto;margin-top:.8rem}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{background:var(--blue);color:#fff;text-align:left;padding:.5rem;font-size:.68rem;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
td{padding:.45rem .5rem;border-bottom:1px solid var(--line);white-space:nowrap}
td a{color:inherit}
ul.notes{margin:.8rem 0 0;padding-left:1.1rem;color:var(--muted);font-size:.82rem;display:grid;gap:.35rem}
@media (max-width:560px){.stats{grid-template-columns:1fr 1fr;gap:.7rem}.stat{padding:.9rem}.stat .big{font-size:1.6rem}
.crest-tile img{height:70px}.banner{padding:1.4rem 1rem 1.5rem}.banner-inner{gap:1rem}.banner p{font-size:.78rem}
.grid2,.grid3{grid-template-columns:1fr}}
footer{max-width:1120px;margin:0 auto;padding:1.5rem 1.2rem 3rem;display:flex;align-items:center;gap:.9rem;border-top:1px solid var(--line)}
footer img{height:44px}
footer span{color:var(--heading);font-weight:700;letter-spacing:.06em}
@media print{nav,details,.post:hover{display:none}.banner,.chip,.badge,.rank,th{-webkit-print-color-adjust:exact;print-color-adjust:exact}
.card,.stat,.post,section{break-inside:avoid}body{background:#fff}}
"""

JS = """
(function () {
  if (!window.Chart) {
    document.querySelectorAll('.chart').forEach(function (el) {
      el.innerHTML = '<p class="note">Charts could not load. Check your connection and refresh.</p>';
    });
    return;
  }
  var root = getComputedStyle(document.documentElement);
  var text = root.getPropertyValue('--text').trim() || '#1a1a1a';
  var cardBg = root.getPropertyValue('--card-bg').trim() || '#f5f4fa';
  Chart.defaults.font.family = "'Supreme', system-ui, sans-serif";
  Chart.defaults.color = text;
  Chart.defaults.borderColor = 'rgba(128,128,128,0.18)';
  Chart.register({
    id: 'centerText',
    afterDraw: function (chart, args, opts) {
      if (!opts || !opts.text) return;
      var a = chart.chartArea, ctx = chart.ctx;
      var x = (a.left + a.right) / 2, y = (a.top + a.bottom) / 2;
      ctx.save();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = text;
      ctx.font = "700 22px 'Supreme', system-ui, sans-serif";
      ctx.fillText(opts.text, x, opts.sub ? y - 9 : y);
      if (opts.sub) {
        ctx.font = "400 11px 'Supreme', system-ui, sans-serif";
        ctx.fillText(opts.sub, x, y + 12);
      }
      ctx.restore();
    }
  });
  CHARTS.forEach(function (c) {
    var el = document.getElementById(c.id);
    if (!el) return;
    if (c.config.type === 'doughnut') {
      c.config.data.datasets.forEach(function (d) { d.borderColor = cardBg; });
    }
    new Chart(el, c.config);
  });
})();
"""

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Leamington RFC · Social Media Dashboard</title>
<link rel="stylesheet" href="https://api.fontshare.com/v2/css?f[]=supreme@400,700&display=swap">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>%%CSS%%</style>
</head>
<body>
<header class="banner"><div class="banner-inner">
  <div class="crest-tile"><img src="data:image/png;base64,%%CREST_BLUE%%" alt="Leamington RFC centenary crest"></div>
  <div>
    <div class="kicker">Leamington RFC · 1926 to 2026</div>
    <h1>Social Media Dashboard</h1>
    <p>Updated %%UPDATED%% · Facebook and Instagram · refreshed automatically every morning</p>
  </div>
</div></header>
<nav><div class="inner">
  <a href="#overview">Overview</a><a href="#audience">Audience</a><a href="#content">What works</a>
  <a href="#club">One Club, One Leam</a><a href="#posts">Posts</a><a href="#notes">About</a>
</div></nav>
<main>
%%BODY%%
</main>
<footer><img src="data:image/png;base64,%%CREST_BLUE%%" alt=""><span>ONE CLUB, ONE LEAM</span></footer>
<script>
var CHARTS = %%CHARTS%%;
%%JS%%
</script>
</body>
</html>"""


def render(account_snapshots, posts, insights, crest_blue, today=None):
    today = today or datetime.now(timezone.utc).date()
    cur_start = today - timedelta(days=PERIOD_DAYS)
    prev_start = cur_start - timedelta(days=PERIOD_DAYS)

    tracked = [p for p in posts.values() if p.get("snapshots")]
    cur_posts = [p for p in tracked if cur_start <= published_date(p) < today]
    prev_posts = [p for p in tracked if prev_start <= published_date(p) < cur_start]

    section_rows = group_stats(cur_posts, lambda p: classify_section(p.get("caption")))
    theme_rows = group_stats(cur_posts, lambda p: classify_category(p.get("caption")))
    format_rows = group_stats(cur_posts, format_label)
    day_rows = group_stats(cur_posts, lambda p: p.get("day_of_week") or "Unknown")

    charts = []
    lines = insight_lines(insights, cur_posts, section_rows, theme_rows, format_rows, day_rows)
    body = "\n".join([
        overview_section(insights, cur_posts, prev_posts, lines, charts),
        audience_section(account_snapshots, insights, charts),
        content_section(cur_posts, format_rows, day_rows, charts),
        club_section(cur_posts, section_rows, theme_rows, charts),
        posts_section(cur_posts, tracked, today),
        notes_section(insights, today),
    ])

    updated = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    charts_json = json.dumps(charts).replace("</", "<\\/")
    return (PAGE.replace("%%CSS%%", CSS)
                .replace("%%JS%%", JS)
                .replace("%%CREST_BLUE%%", crest_blue)
                .replace("%%UPDATED%%", updated)
                .replace("%%BODY%%", body)
                .replace("%%CHARTS%%", charts_json))


def main():
    html = render(
        load_json(ACCOUNT_FILE),
        load_json(POSTS_FILE),
        load_json(INSIGHTS_FILE),
        read_text(CREST_BLUE_FILE),
    )
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
