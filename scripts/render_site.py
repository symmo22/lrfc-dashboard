#!/usr/bin/env python3
"""
Regenerates docs/index.html from the files in data/.

Pure local file processing. The browser loads Chart.js and the Supreme
font when someone opens the page.

Reads:
  data/account_snapshots.json  daily follower counts (ingest.py)
  data/posts.json              per-post lifetime metrics (ingest_posts.py)
  data/insights.json           30/90-day totals and audience (ingest_insights.py)
  data/history.json            the dashboard's own saved daily history (ingest_history.py)
Any of these may be missing or partial; every panel degrades to a plain
note rather than breaking the page.

Period toggle: every period-dependent panel is rendered once per window
(30 and 90 days) and the page shows one at a time. Charts for a window are
only drawn when that window is first shown, because Chart.js can't size a
chart inside a hidden element.

Honesty rules:
  - Reach is never added across platforms, or across Instagram's 30-day
    request limit. Views are counts, so they can be.
  - Posts are always shown per platform.
  - Figures Meta doesn't provide for a window show as "—" with the reason.
  - HISTORICAL_ANCHORS are figures given by the Communications Manager,
    shown as "then vs now", never plotted as if measured daily.
  - Auto-written insights only appear when enough posts sit behind them.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify import SECTIONS, THEMES, classify_section, classify_theme  # noqa: E402
from ingest_posts import LOOKBACK_DAYS  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA_DIR = ROOT / "data"
ACCOUNT_FILE = DATA_DIR / "account_snapshots.json"
POSTS_FILE = DATA_DIR / "posts.json"
INSIGHTS_FILE = DATA_DIR / "insights.json"
HISTORY_FILE = DATA_DIR / "history.json"
OUTPUT_FILE = ROOT / "docs" / "index.html"
CREST_FILE = HERE / "crest_b64.txt"

PERIODS = [30, 90]
DEFAULT_PERIOD = 30
MIN_SAMPLE = 3
QUIET_MIN_AGE_DAYS = 7

HISTORICAL_ANCHORS = {
    "facebook": {"date": "2025-08-01", "count": 1800, "approx": True},
    "instagram": {"date": "2025-08-01", "count": 1043, "approx": False},
}

BRAND = {"blue": "#3E3787", "yellow": "#FAE226", "red": "#CF3B41", "green": "#60AC3F"}
PALETTE = [BRAND["blue"], BRAND["red"], BRAND["yellow"], BRAND["green"],
           "#8A84C4", "#E8898D", "#A9D48F", "#E9DB6A", "#26225A", "#9C3035"]
GREY = "#A8A8B3"
PLATFORM_COLOUR = {"instagram": BRAND["red"], "facebook": BRAND["blue"]}
PLATFORM_NAME = {"instagram": "Instagram", "facebook": "Facebook"}

# Instagram Reels and Facebook videos are the same content cross-posted,
# so they share one format label.
FORMAT_LABELS = {"image": "Image", "carousel": "Carousel", "reel": "Reel / video",
                 "video": "Reel / video", "text_or_link": "Text or link", "unknown": "Other"}
FORMAT_ORDER = ["Reel / video", "Carousel", "Image", "Text or link", "Other"]
PRODUCT_LABELS = {"POST": "Posts", "FEED": "Posts", "REEL": "Reels", "STORY": "Stories",
                  "CAROUSEL_CONTAINER": "Carousels", "AD": "Ads", "IGTV": "Video"}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
AGE_ORDER = ["13-17", "18-24", "25-34", "35-44", "45-54", "55-64", "65+"]
GENDER_LABELS = {"F": "Women", "M": "Men", "U": "Not specified"}


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


def published_date(p):
    return datetime.fromisoformat(p["published_at"].replace("Z", "+00:00")).date()


def post_metrics(p):
    """Latest lifetime figures. Falls back to the newest checkpoint for
    records written before 'latest' existed."""
    latest = p.get("latest") or {}
    if latest.get("metrics"):
        return latest["metrics"]
    snaps = p.get("snapshots") or {}
    return snaps[max(snaps)] if snaps else {}


def is_measured(p):
    return bool(post_metrics(p))


def post_reach(p):
    m = post_metrics(p)
    return m.get("reach") or m.get("post_total_media_view_unique") or 0


def post_views(p):
    m = post_metrics(p)
    return m.get("views") or m.get("post_media_view") or 0


def post_counts(p):
    static = p.get("static_metrics") or {}
    m = post_metrics(p)
    if p["platform"] == "instagram":
        return static.get("likes"), static.get("comments"), m.get("shares"), m.get("saved")
    return static.get("reactions"), static.get("comments"), static.get("shares"), None


def post_interactions(p):
    return sum(v or 0 for v in post_counts(p))


def post_engagement_rate(p):
    reach = post_reach(p)
    return round(100 * post_interactions(p) / reach, 1) if reach else None


def format_label(p):
    return FORMAT_LABELS.get(p.get("format"), (p.get("format") or "Other").title())


def section_of(p):
    return classify_section(p.get("caption"))[0]


def theme_of(p):
    return classify_theme(p.get("caption"))[0]


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
        rows.append({"key": key, "count": len(ps),
                     "avg_reach": round(sum(reaches) / len(reaches)) if reaches else 0,
                     "views": sum(post_views(p) for p in ps)})
    return sorted(rows, key=lambda r: r["count"], reverse=True)


def window_insights(insights, days):
    windows = insights.get("windows")
    if windows is not None:
        return windows.get(str(days)) or {}
    # insights.json written before the period toggle existed: 30 days only
    if days == 30 and "instagram" in insights:
        fb = insights.get("facebook") or {}
        cur, prior = dict(fb.get("current") or {}), dict(fb.get("prior") or {})
        for d in (cur, prior):
            if "reach_28d" in d:
                d["reach"] = d.pop("reach_28d")
        return {"instagram": insights["instagram"],
                "facebook": {"current": cur, "prior": prior, "reach_days": 28}}
    return {}


def demographics_of(insights):
    return insights.get("demographics") or (insights.get("instagram") or {}).get("demographics") or {}


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
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def plural(n, word):
    if n is None:
        return f"<b>—</b> {word}s"
    return f"<b>{n:,}</b> {word}" + ("" if n == 1 else "s")


def stable_colours(labels, reference):
    out = []
    for i, label in enumerate(labels):
        if label in reference:
            out.append(PALETTE[reference.index(label) % len(PALETTE)])
        else:
            out.append(PALETTE[(len(reference) + i) % len(PALETTE)])
    return out


# ---------------------------------------------------------------------------
# Chart configs
# ---------------------------------------------------------------------------

def donut(cid, labels, values, colours, centre="", sub=""):
    return {"id": cid, "config": {
        "type": "doughnut",
        "data": {"labels": labels, "datasets": [{"data": values, "backgroundColor": colours, "borderWidth": 2}]},
        "options": {"responsive": True, "maintainAspectRatio": False, "cutout": "64%",
                    "plugins": {"legend": {"position": "bottom", "labels": {"boxWidth": 12, "padding": 10}},
                                "centerText": {"text": centre, "sub": sub}}}}}


def bars(cid, labels, values, colours, horizontal=False, label=""):
    value_axis, category_axis = ("x", "y") if horizontal else ("y", "x")
    return {"id": cid, "config": {
        "type": "bar",
        "data": {"labels": labels, "datasets": [{"label": label, "data": values, "backgroundColor": colours,
                                                 "borderRadius": 6, "maxBarThickness": 44}]},
        "options": {"indexAxis": "y" if horizontal else "x", "responsive": True, "maintainAspectRatio": False,
                    "plugins": {"legend": {"display": False}},
                    "scales": {category_axis: {"grid": {"display": False}}, value_axis: {"beginAtZero": True}}}}}


# ---------------------------------------------------------------------------
# HTML building blocks
# ---------------------------------------------------------------------------

def stat_card(title, big, sub="", delta="", accent=None, hero=False):
    style = f" style='border-top-color:{accent}'" if accent else ""
    cls = "stat hero" if hero else "stat"
    sub_html = f"<div class='sub'>{sub}</div>" if sub else ""
    return f"<div class='{cls}'{style}><h3>{title}</h3><div class='big'>{big}</div>{sub_html}{delta}</div>"


def chart_card(title, note, cid, aria, tall=False):
    note_html = f"<p class='note'>{note}</p>" if note else ""
    return (f"<div class='card'><h3>{title}</h3>{note_html}<div class='chart{' tall' if tall else ''}'>"
            f"<canvas id='{cid}' role='img' aria-label='{escape(aria, quote=True)}'></canvas></div></div>")


def empty_card(title, message):
    return f"<div class='card'><h3>{title}</h3><p class='note'>{message}</p></div>"


def period_block(days, inner):
    return f"<div class='p p{days}'>{inner}</div>"


def post_card(p, rank=None):
    platform = p["platform"]
    image = p.get("image")
    img_html = ""
    if image:
        img_html = (f"<img src='{escape(image, quote=True)}' alt='' loading='lazy' "
                    "onerror=\"this.closest('.thumb').classList.add('noimg');this.remove()\">")
    rank_html = f"<span class='rank'>{rank}</span>" if rank else ""
    caption = escape((p.get("caption") or "").strip() or "(no caption)")
    likes, comments, shares, _ = post_counts(p)
    link = escape(p.get("permalink") or "#", quote=True)
    return f"""<a class='post' href='{link}' target='_blank' rel='noopener'>
  <div class='{"thumb" if image else "thumb noimg"}'>{img_html}<span class='badge' style='background:{PLATFORM_COLOUR[platform]}'>{PLATFORM_NAME[platform]}</span>{rank_html}</div>
  <div class='body'>
    <div class='date'>{published_date(p).strftime("%a %d %b")} · {escape(format_label(p))} · {escape(section_of(p))}</div>
    <div class='cap'>{caption}</div>
    <div class='nums'><span><b>{num(post_reach(p))}</b> reach</span><span><b>{num(post_views(p))}</b> views</span>
    <span>{plural(likes, 'like')}</span><span>{plural(comments, 'comment')}</span><span>{plural(shares, 'share')}</span></div>
  </div>
</a>"""


def platform_growth_card(snapshots, platform):
    colour, name = PLATFORM_COLOUR[platform], PLATFORM_NAME[platform]
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
        when = datetime.fromisoformat(anchor["date"]).strftime("%B %Y")
        story = (f"<div class='story'>{'~' if anchor.get('approx') else ''}{anchor['count']:,} → {cur:,} "
                 f"since {when} <strong>{'+' if growth >= 0 else ''}{growth}%</strong></div>")
    return (f"<div class='stat' style='border-top-color:{colour}'><h3>{name} followers</h3>"
            f"<div class='big'>{num(cur)}</div>{delta}{story}</div>")


# ---------------------------------------------------------------------------
# One period's worth of analysis
# ---------------------------------------------------------------------------

class Period:
    def __init__(self, days, insights, measured, today):
        self.days = days
        self.label = f"{days} days"
        self.vs = f"vs previous {days} days"
        self.ins = window_insights(insights, days)
        start = today - timedelta(days=days)
        prev_start = start - timedelta(days=days)
        self.cur = [p for p in measured if start <= published_date(p) < today]
        self.prev = [p for p in measured if prev_start <= published_date(p) < start]
        self.sections = group_stats(self.cur, section_of)
        self.themes = group_stats(self.cur, theme_of)
        self.formats = group_stats(self.cur, format_label)
        self.weekdays = group_stats(self.cur, lambda p: p.get("day_of_week") or "Unknown")
        self.placed_by_rule = sum(1 for p in self.cur if not classify_section(p.get("caption"))[1])
        self.today = today

    def cid(self, base):
        return f"{base}_{self.days}"


def insight_lines(P):
    lines = []
    for platform in ("instagram", "facebook"):
        block = P.ins.get(platform) or {}
        cur = (block.get("current") or {}).get("views")
        prev = (block.get("prior") or {}).get("views")
        change = pct_change(cur, prev)
        if change is not None and abs(change) >= 5:
            lines.append(f"<strong>{PLATFORM_NAME[platform]} views are {'up' if change > 0 else 'down'} "
                         f"{abs(change)}%</strong> on the previous {P.label} ({cur:,} against {prev:,}).")

    split = (P.ins.get("instagram") or {}).get("views_by_follower_type") or {}
    nf, f = split.get("NON_FOLLOWER"), split.get("FOLLOWER")
    if nf is not None and f is not None and nf + f > 0:
        lines.append(f"<strong>{round(100 * nf / (nf + f))}% of Instagram views came from people who don't "
                     "follow the club.</strong> That's where new players, parents and supporters come from.")

    def compare(rows, suffix):
        usable = [r for r in rows if r["count"] >= MIN_SAMPLE and r["avg_reach"] > 0]
        if len(usable) < 2:
            return
        best = max(usable, key=lambda r: r["avg_reach"])
        worst = min(usable, key=lambda r: r["avg_reach"])
        ratio = best["avg_reach"] / worst["avg_reach"]
        if ratio >= 1.3:
            lines.append(f"<strong>{escape(best['key'])} {suffix} averaged {best['avg_reach']:,} reach</strong>, "
                         f"{ratio:.1f}× {escape(worst['key'])} {suffix} ({worst['avg_reach']:,}).")

    compare(P.formats, "posts")
    compare(P.themes, "posts")

    usable_days = [r for r in P.weekdays if r["count"] >= MIN_SAMPLE]
    if len(usable_days) >= 2:
        best = max(usable_days, key=lambda r: r["avg_reach"])
        lines.append(f"<strong>{best['key']} posts reach the most people</strong>, averaging "
                     f"{best['avg_reach']:,} across {best['count']} posts.")

    if P.cur:
        present = {r["key"] for r in P.sections}
        missing = [s for s in SECTIONS if s != "Whole Club" and s not in present]
        if missing:
            lines.append(f"<strong>Nothing in the last {P.label} featured {escape(join_names(missing))}.</strong> "
                         "Worth a look for One Club, One Leam balance.")
        share = round(100 * P.placed_by_rule / len(P.cur))
        if share >= 25:
            lines.append(f"{share}% of posts didn't name a team in the caption, so they were placed by rule "
                         "(match posts as 1st XV, the rest as Whole Club). Naming the team makes this exact.")
    return lines


def overview_block(P, charts):
    ig, fb = P.ins.get("instagram") or {}, P.ins.get("facebook") or {}
    igc, igp = ig.get("current") or {}, ig.get("prior") or {}
    fbc, fbp = fb.get("current") or {}, fb.get("prior") or {}
    ig_views, fb_views = igc.get("views"), fbc.get("views")

    present = [(v, prev.get("views")) for v, prev in ((ig_views, igp), (fb_views, fbp)) if v is not None]
    total = sum(v for v, _ in present) if present else None
    total_prev = sum(p for _, p in present) if present and all(p is not None for _, p in present) else None
    no_ig_prior = (ig.get("unavailable") or {}).get("prior")

    if P.days <= 30:
        ig_reach = stat_card("Instagram reach", num(igc.get("reach")), "accounts that saw your content",
                             delta_html(igc.get("reach"), igp.get("reach"), P.vs), BRAND["red"])
    else:
        ig_reach = stat_card("Instagram reach", "—", "Meta only reports Instagram reach for up to 30 days",
                             "", BRAND["red"])
    reach_days = fb.get("reach_days")
    if reach_days:
        fb_reach = stat_card("Facebook reach", num(fbc.get("reach")), f"people who saw your content, {reach_days} days",
                             delta_html(fbc.get("reach"), fbp.get("reach"), f"vs previous {reach_days} days"),
                             BRAND["blue"])
    else:
        fb_reach = stat_card("Facebook reach", "—", "Meta only reports Facebook reach for up to 28 days",
                             "", BRAND["blue"])

    ig_count = sum(1 for p in P.cur if p["platform"] == "instagram")
    fb_count = sum(1 for p in P.cur if p["platform"] == "facebook")
    cur_int = sum(post_interactions(p) for p in P.cur)
    prev_int = sum(post_interactions(p) for p in P.prev)

    total_sub = f"Instagram {num(ig_views)} · Facebook {num(fb_views)}"
    if no_ig_prior:
        total_sub += "<br>No comparison: Instagram keeps 90 days of account data"
    cards = [
        stat_card("Total views", num(total), total_sub,
                  "" if no_ig_prior else delta_html(total, total_prev, P.vs), BRAND["yellow"], hero=True),
        ig_reach,
        fb_reach,
        stat_card("Likes, comments & shares", num(cur_int if P.cur else None), f"on posts published in the last {P.label}",
                  delta_html(cur_int, prev_int, P.vs) if P.cur and len(P.prev) >= MIN_SAMPLE else "", BRAND["green"]),
        stat_card("Posts published",
                  f"<span class='split'><span>{ig_count}<small>Instagram</small></span>"
                  f"<span>{fb_count}<small>Facebook</small></span></span>", f"last {P.label}", "", BRAND["yellow"]),
    ]
    follows = ig.get("follows") or {}
    if "FOLLOWER" in follows and "NON_FOLLOWER" in follows:
        net = follows["FOLLOWER"] - follows["NON_FOLLOWER"]
        cards.append(stat_card("Instagram net followers", f"{'+' if net >= 0 else ''}{net:,}",
                               f"{follows['FOLLOWER']:,} followed · {follows['NON_FOLLOWER']:,} unfollowed", "",
                               BRAND["green"]))
    taps = igc.get("profile_links_taps")
    if taps is not None:
        cards.append(stat_card("Instagram link taps", num(taps), "taps on the website and contact links in the profile",
                               delta_html(taps, igp.get("profile_links_taps"), P.vs), BRAND["red"]))

    if ig_views is not None and fb_views is not None and ig_views + fb_views > 0:
        charts.append(donut(P.cid("viewsSplit"), ["Instagram", "Facebook"], [ig_views, fb_views],
                            [BRAND["red"], BRAND["blue"]], f"{total:,}", f"views, {P.label}"))
        split_html = chart_card("Where views came from", f"Last {P.label}, both platforms", P.cid("viewsSplit"),
                                "Donut chart of views by platform")
    else:
        split_html = empty_card("Where views came from", "Waiting for totals from both platforms.")

    lines = insight_lines(P)
    insight_html = ("<ul class='insights'>" + "".join(f"<li>{l}</li>" for l in lines) + "</ul>") if lines else \
        "<p class='note'>Not enough posts in this period for reliable observations.</p>"

    return f"""<h2>The last {P.label}</h2>
  <p class='lede'>Headline numbers against the {P.label} before. Reach is shown per platform because the same person can follow both; views can be added together.</p>
  <div class='stats'>{''.join(cards)}</div>
  <div class='grid-insights'>
    <div class='card'><h3>What the numbers say</h3><p class='note'>Written automatically from the data on each run.</p>{insight_html}</div>
    {split_html}
  </div>"""


def watching_block(P, charts):
    ig = P.ins.get("instagram") or {}
    out = []
    split = ig.get("views_by_follower_type") or {}
    nf, f = split.get("NON_FOLLOWER"), split.get("FOLLOWER")
    if nf is not None and f is not None and nf + f > 0:
        charts.append(donut(P.cid("followerSplit"), ["Followers", "Not following yet"], [f, nf],
                            [BRAND["blue"], BRAND["yellow"]], f"{round(100 * nf / (nf + f))}%", "non-followers"))
        out.append(chart_card("Who's watching on Instagram", f"Views from followers vs people who don't follow yet, {P.label}",
                              P.cid("followerSplit"), "Donut chart of followers vs non-followers"))
    else:
        out.append(empty_card("Who's watching on Instagram", "Not returned by Meta on the last run."))
    product = {PRODUCT_LABELS.get(k, k.replace("_", " ").title()): v
               for k, v in (ig.get("views_by_product_type") or {}).items() if v}
    if product:
        labels = sorted(product, key=product.get, reverse=True)
        values = [product[k] for k in labels]
        charts.append(donut(P.cid("productSplit"), labels, values, PALETTE[:len(labels)],
                            f"{sum(values):,}", "Instagram views"))
        out.append(chart_card("What they watched", f"Instagram views by content type, {P.label}, Stories and ads included",
                              P.cid("productSplit"), "Donut chart of views by content type"))
    else:
        out.append(empty_card("What they watched", "Not returned by Meta on the last run."))
    return f"<div class='grid2'>{''.join(out)}</div>"


def content_block(P, charts):
    if not P.cur:
        return f"<p class='note'>No posts in the last {P.label}.</p>"
    rows = sorted(P.formats, key=lambda r: FORMAT_ORDER.index(r["key"]) if r["key"] in FORMAT_ORDER else 99)
    labels = [r["key"] for r in rows]
    colours = stable_colours(labels, FORMAT_ORDER)
    total_views = sum(r["views"] for r in rows)
    fmt_share = empty_card("Views by format", "No view figures on these posts yet.")
    if total_views:
        charts.append(donut(P.cid("formatViews"), labels, [r["views"] for r in rows], colours,
                            f"{total_views:,}", "views on these posts"))
        fmt_share = chart_card("Views by format", f"Posts published in the last {P.label}, both platforms",
                               P.cid("formatViews"), "Donut chart of views by format")
    ranked = sorted(rows, key=lambda r: r["avg_reach"], reverse=True)
    charts.append(bars(P.cid("formatReach"), [f"{r['key']} ({r['count']})" for r in ranked],
                       [r["avg_reach"] for r in ranked], stable_colours([r["key"] for r in ranked], FORMAT_ORDER),
                       horizontal=True, label="Average reach"))
    fmt_perf = chart_card("Average reach per post, by format", "Posts in brackets", P.cid("formatReach"),
                          "Bar chart of average reach by format")

    by_day = {r["key"]: r for r in P.weekdays}
    days = [d for d in DAYS if d in by_day]
    solid = [d for d in days if by_day[d]["count"] >= MIN_SAMPLE]
    best = max((by_day[d]["avg_reach"] for d in solid), default=None)
    charts.append(bars(P.cid("dayReach"), [f"{d[:3]} ({by_day[d]['count']})" for d in days],
                       [by_day[d]["avg_reach"] for d in days],
                       [BRAND["yellow"] if d in solid and by_day[d]["avg_reach"] == best else BRAND["blue"] for d in days],
                       label="Average reach"))
    day_perf = chart_card("Best day to post", "Average reach by day published, UK time. Posts in brackets. "
                          "Yellow marks the best day with 3+ posts.", P.cid("dayReach"),
                          "Bar chart of average reach by weekday")
    return f"<div class='grid2'>{fmt_share}{fmt_perf}</div><div class='grid1'>{day_perf}</div>"


def club_block(P, charts):
    counts = {r["key"]: r["count"] for r in P.sections}
    chips = "".join(f"<span class='chip{'' if counts.get(s) else ' zero'}'>{escape(s)}<b>{counts.get(s, 0)}</b></span>"
                    for s in SECTIONS)
    if not P.cur:
        return f"<div class='chips'>{chips}</div><p class='note'>No posts in the last {P.label}.</p>"
    note = (f"<p class='note'>{P.placed_by_rule} of {len(P.cur)} posts didn't name a team, so were placed by rule: "
            "match and senior-squad posts as Men's 1st XV, everything else as Whole Club.</p>") if P.placed_by_rule else ""

    def pair(rows, ref, prefix, title):
        rows = sorted(rows, key=lambda r: ref.index(r["key"]) if r["key"] in ref else 99)
        labels = [r["key"] for r in rows]
        charts.append(donut(P.cid(f"{prefix}Share"), labels, [r["count"] for r in rows],
                            stable_colours(labels, ref), str(sum(r["count"] for r in rows)), "posts"))
        ranked = sorted(rows, key=lambda r: r["avg_reach"], reverse=True)
        charts.append(bars(P.cid(f"{prefix}Reach"), [f"{r['key']} ({r['count']})" for r in ranked],
                           [r["avg_reach"] for r in ranked], stable_colours([r["key"] for r in ranked], ref),
                           horizontal=True, label="Average reach"))
        return (chart_card(f"{title}: share of posts", f"Last {P.label}, both platforms", P.cid(f"{prefix}Share"),
                           f"Donut chart of posts by {title.lower()}")
                + chart_card(f"{title}: average reach", "Posts in brackets. Read little into groups under 3.",
                             P.cid(f"{prefix}Reach"), f"Bar chart of average reach by {title.lower()}", tall=True))

    return (f"<div class='chips'>{chips}</div>{note}"
            f"<div class='grid2'>{pair(P.sections, SECTIONS, 'section', 'Section')}</div>"
            f"<div class='grid2'>{pair(P.themes, THEMES, 'theme', 'Theme')}</div>")


def posts_block(P):
    top = sorted(P.cur, key=post_reach, reverse=True)[:6]
    top_ids = {id(p) for p in top}
    quiet = sorted([p for p in P.cur if id(p) not in top_ids
                    and (P.today - published_date(p)).days >= QUIET_MIN_AGE_DAYS], key=post_reach)[:3]
    html = ("<div class='posts'>" + "".join(post_card(p, i + 1) for i, p in enumerate(top)) + "</div>") if top \
        else f"<p class='note'>No posts in the last {P.label}.</p>"
    if quiet:
        html += ("<h3 class='sub-h'>Quieter posts worth learning from</h3>"
                 "<p class='note'>Lowest reach among posts at least a week old.</p>"
                 "<div class='posts'>" + "".join(post_card(p) for p in quiet) + "</div>")
    return html


# ---------------------------------------------------------------------------
# Period-independent sections
# ---------------------------------------------------------------------------

def growth_block(snapshots, charts):
    by_platform = {"facebook": {}, "instagram": {}}
    for r in snapshots.values():
        c = follower_count(r, r["platform"])
        if c is not None:
            by_platform[r["platform"]][r["date"]] = c
    dates = sorted({d for m in by_platform.values() for d in m})
    cards = platform_growth_card(snapshots, "instagram") + platform_growth_card(snapshots, "facebook")
    if len(dates) < 2:
        return (f"<div class='stats two'>{cards}</div>"
                + empty_card("Followers, day by day", "Fills in day by day. The growth since August 2025 above uses "
                             "the figures you gave directly."))
    charts.append({"id": "growthChart", "config": {
        "type": "line",
        "data": {"labels": dates, "datasets": [
            {"label": "Instagram (left scale)", "data": [by_platform["instagram"].get(d) for d in dates],
             "borderColor": BRAND["red"], "backgroundColor": BRAND["red"], "cubicInterpolationMode": "monotone",
             "spanGaps": True, "yAxisID": "yIG", "pointRadius": 3},
            {"label": "Facebook (right scale)", "data": [by_platform["facebook"].get(d) for d in dates],
             "borderColor": BRAND["blue"], "backgroundColor": BRAND["blue"], "cubicInterpolationMode": "monotone",
             "spanGaps": True, "yAxisID": "yFB", "pointRadius": 3}]},
        "options": {"responsive": True, "maintainAspectRatio": False, "plugins": {"legend": {"position": "bottom"}},
                    "scales": {"x": {"grid": {"display": False}},
                               "yIG": {"position": "left", "ticks": {"color": BRAND["red"], "precision": 0}},
                               "yFB": {"position": "right", "ticks": {"color": BRAND["blue"], "precision": 0},
                                       "grid": {"drawOnChartArea": False}}}}}})
    return (f"<div class='stats two'>{cards}</div><div class='grid1'>"
            + chart_card("Followers, day by day", f"Tracked daily since {dates[0]}. Each platform has its own scale "
                         "so small changes show.", "growthChart", "Line chart of follower counts", tall=True)
            + "</div>")


def demographics_block(insights, charts):
    demo = demographics_of(insights)
    out = []
    age = (demo.get("age") or {}).get("values") or {}
    if age:
        labels = [a for a in AGE_ORDER if a in age] + [a for a in age if a not in AGE_ORDER]
        charts.append(bars("ageChart", labels, [age[a] for a in labels], BRAND["blue"], label="Followers"))
        out.append(chart_card("Age", "Instagram followers", "ageChart", "Bar chart of followers by age"))
    gender = (demo.get("gender") or {}).get("values") or {}
    if gender:
        labels = [GENDER_LABELS.get(k, k) for k in gender]
        values = list(gender.values())
        colours = [{"Women": BRAND["red"], "Men": BRAND["blue"]}.get(l, GREY) for l in labels]
        top = labels[values.index(max(values))]
        charts.append(donut("genderChart", labels, values, colours,
                            f"{round(100 * max(values) / sum(values))}%", top.lower()))
        out.append(chart_card("Gender", "Instagram followers", "genderChart", "Donut chart of followers by gender"))
    city = (demo.get("city") or {}).get("values") or {}
    if city:
        top = sorted(city.items(), key=lambda kv: kv[1], reverse=True)[:8]
        charts.append(bars("cityChart", [k.split(",")[0] for k, _ in top], [v for _, v in top],
                           BRAND["red"], horizontal=True, label="Followers"))
        out.append(chart_card("Top towns and cities", "Instagram followers", "cityChart", "Bar chart of followers by city"))
    if not out:
        out.append(empty_card("Audience", "Meta didn't return follower demographics on the last run. "
                                          "Facebook no longer shares age or gender at all."))
    return f"<div class='grid3'>{''.join(out)}</div>"


def table_block(measured):
    rows = sorted(measured, key=post_reach, reverse=True)[:120]
    body = ""
    for p in rows:
        likes, comments, shares, saves = post_counts(p)
        rate = post_engagement_rate(p)
        section, named = classify_section(p.get("caption"))
        caption = escape((p.get("caption") or "")[:70] + ("…" if len(p.get("caption") or "") > 70 else ""))
        body += (f"<tr><td>{published_date(p).isoformat()}</td><td>{PLATFORM_NAME[p['platform']]}</td>"
                 f"<td>{escape(format_label(p))}</td><td>{escape(section)}{'' if named else ' *'}</td>"
                 f"<td>{escape(theme_of(p))}</td><td>{num(post_reach(p))}</td><td>{num(post_views(p))}</td>"
                 f"<td>{num(likes)}</td><td>{num(comments)}</td><td>{num(shares)}</td><td>{num(saves)}</td>"
                 f"<td>{'—' if rate is None else str(rate) + '%'}</td>"
                 f"<td><a href='{escape(p.get('permalink') or '#', quote=True)}' target='_blank' rel='noopener'>{caption}</a></td></tr>")
    return f"""<details>
    <summary>Every tracked post, last {LOOKBACK_DAYS} days ({len(measured)})</summary>
    <p class='note'>* team not named in the caption, placed by rule.</p>
    <div class='table-wrap'><table>
      <thead><tr><th>Date</th><th>Platform</th><th>Format</th><th>Section</th><th>Theme</th><th>Reach</th><th>Views</th>
      <th>Likes</th><th>Comments</th><th>Shares</th><th>Saves</th><th>Eng. rate</th><th>Caption</th></tr></thead>
      <tbody>{body}</tbody>
    </table></div>
  </details>"""


def history_note(history):
    daily = history.get("daily") or {}
    parts = []
    for platform, name in (("instagram", "Instagram"), ("facebook", "Facebook")):
        days = sorted(k for k, v in (daily.get(platform) or {}).items() if not v.get("_incomplete"))
        if days:
            parts.append(f"{name} from {datetime.fromisoformat(days[0]).strftime('%d %B %Y')} ({len(days):,} days)")
    if not parts:
        return "The dashboard's own daily history starts on the next run. It powers the 12-month view, coming next."
    return ("Saved daily history so far: " + " · ".join(parts) + ". This powers the 12-month view, coming next, "
            "and keeps figures Meta later deletes.")


def notes_block(insights, unmeasured, today, history=None):
    notes = [
        "Instagram gives reach for up to 30 days, so the 90-day view shows views but not Instagram reach. "
        "Instagram also keeps account data for 90 days, so the 90-day view has no previous-period comparison yet.",
        "Facebook reach uses Meta's own 28-day figure, because unique people can't be added up day by day.",
        "Reach is never added across platforms: the same person can follow both. Views can be, so total views combines them.",
        "Post figures are lifetime totals, refreshed every morning.",
        "Posts shared into Facebook groups, and ads built directly in Ads Manager, don't appear on the Page and aren't "
        "included in post figures. Instagram ad views do appear in the 'What they watched' chart.",
        "Section and theme are read from caption text. Posts that don't name a team are placed by rule and marked * in the table.",
        "Growth since August 2025 uses figures supplied by the Communications Manager (Facebook rounded to 1,800). "
        "Daily tracking began when this dashboard went live.",
    ]
    notes.append(history_note(history or {}))
    if unmeasured:
        notes.append(f"{unmeasured} post(s) are listed but haven't been measured yet. They're measured in batches over the next few runs.")
    generated = insights.get("generated_at")
    if not generated:
        notes.append("Period totals and audience data haven't been collected yet. They appear after the next run.")
    else:
        age = (today - datetime.fromisoformat(generated).date()).days
        if age > 2:
            notes.append(f"Period totals were last refreshed {age} days ago.")
    for n in insights.get("notes") or []:
        notes.append("Not available on the last run: " + escape(n.split(":")[0]) + ".")
    return "<ul class='notes'>" + "".join(f"<li>{n}</li>" for n in notes) + "</ul>"


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

CSS = open(HERE / "dashboard.css", encoding="utf-8").read() if (HERE / "dashboard.css").exists() else ""

JS = """
(function () {
  var hasChart = !!window.Chart;
  var made = {};
  if (hasChart) {
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
  } else {
    document.querySelectorAll('.chart').forEach(function (el) {
      el.innerHTML = '<p class="note">Charts could not load. Check your connection and refresh.</p>';
    });
  }
  function build(key) {
    if (!hasChart || made[key]) return;
    made[key] = true;
    (CHARTS[key] || []).forEach(function (c) {
      var el = document.getElementById(c.id);
      if (!el) return;
      if (c.config.type === 'doughnut') {
        c.config.data.datasets.forEach(function (d) { d.borderColor = cardBg; });
      }
      new Chart(el, c.config);
    });
  }
  function setPeriod(p) {
    document.body.setAttribute('data-period', p);
    document.querySelectorAll('.period-toggle button').forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-period') === p ? 'true' : 'false');
    });
    build(p);
    try { localStorage.setItem('lrfc-period', p); } catch (e) {}
  }
  document.querySelectorAll('.period-toggle button').forEach(function (b) {
    b.addEventListener('click', function () { setPeriod(b.getAttribute('data-period')); });
  });
  build('common');
  var saved = null;
  try { saved = localStorage.getItem('lrfc-period'); } catch (e) {}
  setPeriod(saved && CHARTS[saved] ? saved : '%%DEFAULT%%');
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
<body data-period="%%DEFAULT%%">
<header class="banner"><div class="banner-inner">
  <div class="crest-tile"><img src="data:image/png;base64,%%CREST%%" alt="Leamington RFC centenary crest"></div>
  <div>
    <div class="kicker">Leamington RFC · 1926 to 2026</div>
    <h1>Social Media Dashboard</h1>
    <p>Updated %%UPDATED%% · Facebook and Instagram · refreshed automatically every morning</p>
  </div>
</div></header>
<nav><div class="inner">
  <div class="period-toggle" role="group" aria-label="Time period">%%TOGGLE%%</div>
  <div class="links"><a href="#overview">Overview</a><a href="#audience">Audience</a><a href="#content">What works</a>
  <a href="#club">One Club, One Leam</a><a href="#posts">Posts</a><a href="#notes">About</a></div>
</div></nav>
<main>
%%BODY%%
</main>
<footer><img src="data:image/png;base64,%%CREST%%" alt=""><span>ONE CLUB, ONE LEAM</span></footer>
<script>
var CHARTS = %%CHARTS%%;
%%JS%%
</script>
</body>
</html>"""


def render(account_snapshots, posts, insights, crest, today=None, history=None):
    today = today or datetime.now(timezone.utc).date()
    all_posts = list(posts.values())
    measured = [p for p in all_posts if is_measured(p)]
    charts = {"common": []}
    periods = [Period(d, insights, measured, today) for d in PERIODS]
    for P in periods:
        charts[str(P.days)] = []

    def per_period(fn):
        return "".join(period_block(P.days, fn(P, charts[str(P.days)])) for P in periods)

    body = f"""
<section id='overview'>{per_period(overview_block)}</section>
<section id='audience'>
  <h2>Audience</h2>
  <p class='lede'>How the following has grown, who is watching, and who they are.</p>
  {growth_block(account_snapshots, charts['common'])}
  {per_period(watching_block)}
  {demographics_block(insights, charts['common'])}
</section>
<section id='content'>
  <h2>What works</h2>
  <p class='lede'>Which formats and days earn the most attention, from posts published in the selected period.</p>
  {per_period(content_block)}
</section>
<section id='club'>
  <h2>One Club, One Leam</h2>
  <p class='lede'>Every section of the club and how often it featured. Red means nothing featured it in the selected period.</p>
  {per_period(club_block)}
</section>
<section id='posts'>
  <h2>Posts</h2>
  <p class='lede'>The furthest-reaching posts of the selected period, lifetime figures. Tap any card to open the post.</p>
  {per_period(lambda P, c: posts_block(P))}
  {table_block(measured)}
</section>
<section id='notes'><h2>About these numbers</h2>{notes_block(insights, len(all_posts) - len(measured), today, history)}</section>
"""
    toggle = "".join(f"<button type='button' data-period='{d}' aria-pressed='{'true' if d == DEFAULT_PERIOD else 'false'}'>"
                     f"{d} days</button>" for d in PERIODS)
    updated = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    return (PAGE.replace("%%CSS%%", CSS)
                .replace("%%JS%%", JS)
                .replace("%%TOGGLE%%", toggle)
                .replace("%%DEFAULT%%", str(DEFAULT_PERIOD))
                .replace("%%CREST%%", crest)
                .replace("%%UPDATED%%", updated)
                .replace("%%BODY%%", body)
                .replace("%%CHARTS%%", json.dumps(charts).replace("</", "<\\/")))


def main():
    crest = CREST_FILE.read_text(encoding="ascii").strip() if CREST_FILE.exists() else ""
    html = render(load_json(ACCOUNT_FILE), load_json(POSTS_FILE), load_json(INSIGHTS_FILE), crest,
                  history=load_json(HISTORY_FILE))
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
