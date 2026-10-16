"""
Executive Committee one-pager and the automatic commentary.

Two things live here:

  build_facts() / commentary()
      Works out what moved in a period, the most likely reason the data
      itself can support, and one specific recommendation for the next
      fortnight. Rule-based: the same data always gives the same words,
      and nothing is stated that isn't a figure Meta returned. Used by the
      main dashboard and by the one-pager.

  render_exec()
      docs/exec.html: one A4 page for the committee, rolling 90 days.
      Charts are plain SVG drawn here, so the page needs no JavaScript,
      prints cleanly and still works if a chart library can't load.

Nothing here calls Meta. It only reads what the ingest scripts saved.
"""

from datetime import date, timedelta
from html import escape

import render_site as rs

EXEC_DAYS = 90
RECENT_DAYS = 30          # coverage gaps and posting volume look at the last 30 days
FORMAT_LEVER_RATIO = 1.5  # a format must beat the most-used one by this much to be recommended
FORMAT_LEVER_MAX_SHARE = 0.34
VOLUME_CHANGE = 1.3

FORMAT_WORDS = {
    "Reel / video": ("a Reel or short video", "Reels and videos"),
    "Carousel": ("a carousel", "Carousels"),
    "Image": ("a single-image post", "Single images"),
    "Text or link": ("a text or link post", "Text and link posts"),
}


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def _ratio(cur, prev):
    return cur / prev if cur and prev else None


def _latest(series):
    """(date_key, value) of the newest entry in a {date: value} dict."""
    if not series:
        return None, None
    key = max(series)
    return key, series[key]


def _shift(key, days):
    return (date.fromisoformat(key) - timedelta(days=days)).isoformat()


def build_facts(P, recent, saved, snapshots, today):
    """Every figure the commentary and the one-pager use, for period P.
    `recent` is the 30-day period, used for coverage gaps and volume."""
    ig = P.ins.get("instagram") or {}
    fb = P.ins.get("facebook") or {}
    igc, igp = ig.get("current") or {}, ig.get("prior") or {}
    fbc, fbp = fb.get("current") or {}, fb.get("prior") or {}
    start = today - timedelta(days=P.days)
    f = {"days": P.days, "start": start, "end": today - timedelta(days=1)}
    # Label the period from the dates Meta's totals actually cover, when known.
    covered = (P.ins.get("range") or {}).get("current")
    if covered:
        f["start"] = date.fromisoformat(covered[0])
        f["end"] = date.fromisoformat(covered[1]) - timedelta(days=1)

    # Views
    f["ig_views"], f["fb_views"] = igc.get("views"), fbc.get("views")
    f["ig_views_prev"], f["fb_views_prev"] = igp.get("views"), fbp.get("views")
    present = [v for v in (f["ig_views"], f["fb_views"]) if v is not None]
    f["total_views"] = sum(present) if present else None
    last_year = [(saved.fb.get((d - timedelta(days=365)).isoformat()) or {}).get("views")
                 for d in rs.day_list(start, today)]
    have = [v for v in last_year if v is not None]
    f["fb_views_last_year"] = sum(have) if len(have) >= 0.95 * P.days else None

    # Followers
    f["ig_followers"] = rs.followers_now(snapshots, "instagram")
    f["fb_followers"] = rs.followers_now(snapshots, "facebook")
    f["ig_followers_then"] = saved.ig_followers.get(start.isoformat())
    f["fb_followers_then"] = saved.fb_followers(start.isoformat())
    f["ig_anchor"] = rs.IG_START_FIGURE
    f["fb_anchor"] = rs.facebook_start_figure(saved)

    # Reach: the newest rolling figure per platform, the one before it, and a year ago
    for platform, series, span in (("fb", saved.fb_reach, 28), ("ig", saved.ig_reach, 30)):
        key, value = _latest(series)
        f[f"{platform}_reach"], f[f"{platform}_reach_to"], f[f"{platform}_reach_span"] = value, key, span
        f[f"{platform}_reach_prev"] = series.get(_shift(key, span)) if key else None
        f[f"{platform}_reach_last_year"] = series.get(_shift(key, 365)) if key else None
    f["ig_multiple"] = _ratio(f["ig_reach"], f["ig_followers"])
    f["fb_multiple"] = _ratio(f["fb_reach"], f["fb_followers"])

    # Beyond followers, and paid
    split = ig.get("views_by_follower_type") or {}
    nf, fo = split.get("NON_FOLLOWER"), split.get("FOLLOWER")
    f["ig_nonfollower_share"] = round(100 * nf / (nf + fo)) if nf is not None and fo is not None and nf + fo else None
    fb_split = saved.fb_split("views_by_is_from_followers", start, today) or {}
    f_nf, f_fo = fb_split.get("0", 0), fb_split.get("1", 0)
    f["fb_nonfollower_share"] = round(100 * f_nf / (f_nf + f_fo)) if f_nf + f_fo else None
    paid = saved.fb_split("views_by_is_from_ads", today - timedelta(days=RECENT_DAYS), today) or {}
    ads, organic = paid.get("1", 0), paid.get("0", 0)
    f["fb_paid_share_recent"] = round(100 * ads / (ads + organic)) if ads + organic else None

    # Facebook follows and unfollows across the period, from saved daily history
    fb_days = [saved.fb.get(d.isoformat()) for d in rs.day_list(start, today)]
    fb_days = [v for v in fb_days if v and "follows" in v]
    if len(fb_days) >= 0.9 * P.days:
        f["fb_follows"] = sum(v.get("follows") or 0 for v in fb_days)
        f["fb_unfollows"] = sum(v.get("unfollows") or 0 for v in fb_days)
    else:
        f["fb_follows"] = f["fb_unfollows"] = None

    # Net change in followers. Meta's own follows minus unfollows where it is
    # held, so this matches the net-followers cards exactly; otherwise the
    # difference between the follower counts at each end of the period.
    ig_flow = ig.get("follows") or {}
    if "FOLLOWER" in ig_flow and "NON_FOLLOWER" in ig_flow:
        f["ig_net"] = ig_flow["FOLLOWER"] - ig_flow["NON_FOLLOWER"]
    elif f["ig_followers"] is not None and f["ig_followers_then"] is not None:
        f["ig_net"] = f["ig_followers"] - f["ig_followers_then"]
    else:
        f["ig_net"] = None
    if f["fb_follows"] is not None:
        f["fb_net"] = f["fb_follows"] - f["fb_unfollows"]
    elif f["fb_followers"] is not None and f["fb_followers_then"] is not None:
        f["fb_net"] = f["fb_followers"] - f["fb_followers_then"]
    else:
        f["fb_net"] = None

    # Response and output
    f["interactions"] = sum(rs.post_interactions(p) for p in P.cur) if P.cur else None
    f["interactions_prev"] = sum(rs.post_interactions(p) for p in P.prev) if len(P.prev) >= rs.MIN_SAMPLE else None
    f["ig_posts"] = sum(1 for p in P.everything if p["platform"] == "instagram")
    f["fb_posts"] = sum(1 for p in P.everything if p["platform"] == "facebook")
    f["pieces"] = len(P.pieces)

    # Volume: pieces in the last 30 days against the 30 before
    recent_start = today - timedelta(days=RECENT_DAYS)
    f["recent_pieces"] = len(recent.pieces)
    f["before_pieces"] = len(rs.pair_pieces(
        [p for p in saved.all_posts if rs.post_metrics(p)
         and recent_start - timedelta(days=RECENT_DAYS) <= rs.published_date(p) < recent_start]))

    # Coverage
    f["coverage"] = {s: P.coverage.get(s, 0) for s in rs.SECTIONS}
    f["missing_recent"] = [s for s in rs.SECTIONS if s != "Whole Club" and not recent.coverage.get(s)]
    f["missing_period"] = [s for s in rs.SECTIONS if s != "Whole Club" and not P.coverage.get(s)]

    # What works
    usable = [r for r in P.formats if r["count"] >= rs.MIN_SAMPLE and r["avg_reach"] > 0]
    f["format_best"] = max(usable, key=lambda r: r["avg_reach"]) if len(usable) >= 2 else None
    f["format_most_used"] = max(usable, key=lambda r: r["count"]) if len(usable) >= 2 else None
    f["measured_posts"] = len(P.cur)
    days_ok = [r for r in P.weekdays if r["count"] >= rs.MIN_SAMPLE]
    f["best_day"] = max(days_ok, key=lambda r: r["avg_reach"]) if len(days_ok) >= 2 else None
    f["top"] = rs.ranked_pieces(P)[:3]
    f["recent_top"] = rs.ranked_pieces(recent)[:1]   # what drove the latest four weeks, not the whole period
    return f


# ---------------------------------------------------------------------------
# Commentary: three sentences and one recommendation
# ---------------------------------------------------------------------------

def _change(cur, prev):
    """'up 12%', 'down 8%' or 'level'. None when it can't be worked out."""
    pct = rs.pct_change(cur, prev)
    if pct is None:
        return None
    if pct == 0:
        return "level"
    return f"{'up' if pct > 0 else 'down'} {abs(pct)}%"


def _times(ratio):
    """Plain words for a ratio: '3.3 times', 'about the same as', '40% below'."""
    if ratio >= 1.15:
        return f"{ratio:.1f} times"
    if ratio > 0.87:
        return "about the same as"
    return f"{round(100 * (1 - ratio))}% below"


def _join(names):
    return rs.join_names([escape(n) for n in names])


def _day(key):
    return f"{date.fromisoformat(key).day} {date.fromisoformat(key):%B}"


def sentence_views(f):
    """What moved: total attention, against the period before and last year."""
    if f["total_views"] is None:
        return None
    text = f"<strong>The club's content was viewed {f['total_views']:,} times in the last {f['days']} days.</strong>"
    fb_prev, fb_year = _change(f["fb_views"], f["fb_views_prev"]), _change(f["fb_views"], f["fb_views_last_year"])
    if fb_prev and _change(f["ig_views"], f["ig_views_prev"]):
        both = _change(f["total_views"], f["fb_views_prev"] + f["ig_views_prev"])
        text += f" That is {both} on the {f['days']} days before"
        text += f", and Facebook is {fb_year} on the same period last year." if fb_year else "."
    elif fb_prev:
        text += f" Facebook is {fb_prev} on the {f['days']} days before"
        text += f" and {fb_year} on the same period last year." if fb_year else "."
    return text


def likely_reasons(f, rising):
    """Reasons the data itself supports, strongest first. Never a guess
    from outside it (the fixture list, the weather, the time of year)."""
    reasons = []
    now, before = f["recent_pieces"], f["before_pieces"]
    if now and before:
        if rising and now >= VOLUME_CHANGE * before:
            reasons.append(f"more content went out ({now} pieces against {before} the month before)")
        elif not rising and before >= VOLUME_CHANGE * now:
            reasons.append(f"less content went out ({now} pieces against {before} the month before)")
    if rising and f["recent_top"]:
        lead = rs.piece_lead(f["recent_top"][0])
        reach = rs.post_reach(lead)
        followers = f["fb_followers"] if lead["platform"] == "facebook" else f["ig_followers"]
        if followers and reach >= 2 * followers:
            reasons.append(f"the top post alone was seen by {reach:,} people")
    if rising and (f["fb_paid_share_recent"] or 0) >= 10:
        reasons.append(f"{f['fb_paid_share_recent']}% of Facebook views were paid for")
    return reasons[:2]


def sentence_reach(f):
    """How many people, against the four weeks before, and the likely reason."""
    bits, rising_votes = [], []
    for platform, name in (("fb", "Facebook"), ("ig", "Instagram")):
        value, prev = f[f"{platform}_reach"], f[f"{platform}_reach_prev"]
        if not value:
            continue
        text = f"{value:,} people on {name}"
        ratio = _ratio(value, prev)
        if ratio:
            text += f" ({_times(ratio)} the four weeks before)"
            rising_votes.append(ratio >= 1)
        bits.append(text)
    if not bits:
        return None
    to = f["fb_reach_to"] or f["ig_reach_to"]
    text = f"<strong>In the four weeks to {_day(to)} it reached {' and '.join(bits)}.</strong>"
    rising = bool(rising_votes) and all(rising_votes)
    falling = bool(rising_votes) and not any(rising_votes)
    if rising or falling:
        reasons = likely_reasons(f, rising)
        text += (" Likely reason: " + " and ".join(reasons) + "." if reasons else
                 " The data doesn't point to a single reason.")
    return text


def sentence_audience(f):
    """Who: growth in the following, and how far beyond it the content went."""
    grew = []
    for platform, name in (("ig", "Instagram"), ("fb", "Facebook")):
        now, diff = f[f"{platform}_followers"], f[f"{platform}_net"]
        if now is not None and diff is not None:
            grew.append(f"{name} {'gained' if diff >= 0 else 'lost'} {abs(diff):,} followers (now {now:,})")
    shares = []
    if f["ig_nonfollower_share"] is not None:
        shares.append(f"{f['ig_nonfollower_share']}% of Instagram views")
    if f["fb_nonfollower_share"] is not None:
        shares.append(f"{f['fb_nonfollower_share']}% of Facebook views")
    parts = []
    if grew:
        parts.append(f"<strong>{' and '.join(grew)}.</strong>")
    if shares:
        parts.append(f"{' and '.join(shares)} came from people who don't follow the club yet.")
    return " ".join(parts) or None


def recommendation(f):
    """One specific action for the next fortnight, from two levers the
    Communications Manager controls: which format to use and which section
    to feature. Combined into a single action when both apply."""
    if not f["recent_pieces"]:
        return (f"<strong>Publish something.</strong> Nothing has gone out in the last {RECENT_DAYS} days, so there "
                "is no recent data to steer by. A whole-club post is the simplest place to start.")
    best, used = f["format_best"], f["format_most_used"]
    lever = None
    if best and used and best["key"] != used["key"] and f["measured_posts"]:
        ratio = best["avg_reach"] / used["avg_reach"]
        if ratio >= FORMAT_LEVER_RATIO and best["count"] / f["measured_posts"] <= FORMAT_LEVER_MAX_SHARE:
            one, many = FORMAT_WORDS.get(best["key"], (f"a {best['key'].lower()} post", f"{best['key']} posts"))
            _, used_many = FORMAT_WORDS.get(used["key"], ("", f"{used['key']} posts"))
            lever = {"one": one, "why": (f"{many} reached {ratio:.1f} times as many people per post as "
                                         f"{used_many.lower()} over the last {f['days']} days, but were only "
                                         f"{best['count']} of {f['measured_posts']} posts")}
    gaps = f["missing_recent"]
    gap_names = _join(gaps[:2]) if gaps else ""
    gap_or = " or ".join(escape(g) for g in gaps[:2])
    if len(gaps) == 1:
        gap_why = f"{escape(gaps[0])} hasn't featured in the last {RECENT_DAYS} days"
    elif len(gaps) == 2:
        gap_why = f"neither {escape(gaps[0])} nor {escape(gaps[1])} has featured in the last {RECENT_DAYS} days"
    elif gaps:
        gap_why = (f"{len(gaps)} sections haven't featured in the last {RECENT_DAYS} days "
                   f"({_join(gaps)}); start with {gap_names}")
    if lever and gaps:
        return (f"<strong>Post {lever['one']} featuring {gap_or}.</strong> {lever['why']}, and {gap_why}.")
    if gaps:
        return (f"<strong>Feature {gap_names} at least once{' each' if len(gaps) > 1 else ''}.</strong> "
                f"{gap_why[0].upper() + gap_why[1:]}, and One Club, One Leam means every section is seen.")
    if lever:
        return f"<strong>Post {lever['one']} at least twice.</strong> {lever['why']}."
    if f["best_day"]:
        d = f["best_day"]
        return (f"<strong>Schedule the fortnight's most important post for a {d['key']}.</strong> {d['key']} posts "
                f"averaged {d['avg_reach']:,} reach across {d['count']} posts, the best of any day.")
    return ("<strong>Keep the current mix.</strong> No format, day or section stands out enough in the data to "
            "justify a change.")


def commentary(f):
    sentences = [s for s in (sentence_views(f), sentence_reach(f), sentence_audience(f)) if s]
    return {"sentences": sentences, "recommendation": recommendation(f)}


# ---------------------------------------------------------------------------
# SVG charts (no JavaScript, print-safe)
# ---------------------------------------------------------------------------

INK, MUTED, GRID = "#1a1a1a", "#62606f", "#dddbe6"
FB, IG = rs.BRAND["blue"], rs.BRAND["red"]


def compact(v):
    if v >= 100000:
        return f"{v / 1000:.0f}k"
    if v >= 1000:
        return f"{v / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{v:,}"


def nice_scale(v):
    """(top, number of steps) for an axis from zero: the smallest of 1, 2,
    2.5, 5, 10 × a power of ten that is >= v, split into clean steps."""
    if v <= 0:
        return 1, 1
    mag = 10 ** (len(str(int(v))) - 1)
    for m, steps in ((1, 5), (2, 4), (2.5, 5), (5, 5), (10, 5)):
        if m * mag >= v:
            return m * mag, steps
    return 10 * mag, 5


def column_path(x, y, w, h, r=2.5):
    """A column with a rounded top and a square foot on the baseline."""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def svg_monthly_reach(months, fb_values, ig_values):
    """Grouped columns: people reached per month, Facebook beside Instagram."""
    W, H, left, right, top, bottom = 360, 122, 30, 6, 14, 17
    plot_w, plot_h = W - left - right, H - top - bottom
    peak, steps = nice_scale(max([v for v in fb_values + ig_values if v] or [1]))
    band = plot_w / len(months)
    bar = min(18, band * 0.34)
    out = [f"<svg viewBox='0 0 {W} {H}' role='img' aria-label='Column chart of people reached each month on "
           f"Facebook and Instagram' xmlns='http://www.w3.org/2000/svg'>"]
    for i in range(steps + 1):
        value = peak * i / steps
        y = top + plot_h - plot_h * i / steps
        out.append(f"<line x1='{left}' x2='{W - right}' y1='{y:.1f}' y2='{y:.1f}' stroke='{GRID}' stroke-width='1'/>")
        out.append(f"<text x='{left - 5}' y='{y + 3:.1f}' text-anchor='end' font-size='8.5' fill='{MUTED}'>{compact(int(value))}</text>")
    for i, month in enumerate(months):
        centre = left + band * (i + 0.5)
        # Labels lean away from the neighbouring column, so a label never sits on top of the other bar.
        for value, colour, offset, name, anchor, edge in ((fb_values[i], FB, -bar - 1, "Facebook", "end", bar),
                                                         (ig_values[i], IG, 1, "Instagram", "start", 0)):
            if not value:
                continue
            h = plot_h * value / peak
            x, y = centre + offset, top + plot_h - h
            out.append(f"<path d='{column_path(x, y, bar, h)}' fill='{colour}'><title>{name}, {month}: {value:,} people</title></path>")
            out.append(f"<text x='{x + edge:.1f}' y='{y - 3:.1f}' text-anchor='{anchor}' font-size='7.5' fill='{INK}'>{compact(value)}</text>")
        out.append(f"<text x='{centre:.1f}' y='{H - 6}' text-anchor='middle' font-size='9' fill='{MUTED}'>{month}</text>")
    out.append("</svg>")
    return "".join(out)


def svg_followers(points):
    """Two lines over the last 12 months. points: list of
    (date, facebook, instagram_real, instagram_estimated)."""
    W, H, left, right, top, bottom = 360, 122, 30, 40, 10, 17
    plot_w, plot_h = W - left - right, H - top - bottom
    values = [v for _, a, b, c in points for v in (a, b, c) if v is not None]
    if not values:
        return ""
    low = (min(values) // 250) * 250
    high = -(-max(values) // 250) * 250
    if high == low:
        high = low + 250
    n = len(points)

    def xy(i, v):
        return left + plot_w * i / max(n - 1, 1), top + plot_h - plot_h * (v - low) / (high - low)

    def path(index):
        d, pen = [], False
        for i, row in enumerate(points):
            v = row[index]
            if v is None:
                pen = False
                continue
            x, y = xy(i, v)
            d.append(f"{'L' if pen else 'M'}{x:.1f},{y:.1f}")
            pen = True
        return " ".join(d)

    out = [f"<svg viewBox='0 0 {W} {H}' role='img' aria-label='Line chart of Facebook and Instagram followers over "
           f"the last 12 months' xmlns='http://www.w3.org/2000/svg'>"]
    steps = int((high - low) / 250)
    stride = 1 if steps <= 5 else 2
    for k in range(0, steps + 1, stride):
        value = low + 250 * k
        _, y = xy(0, value)
        out.append(f"<line x1='{left}' x2='{W - right}' y1='{y:.1f}' y2='{y:.1f}' stroke='{GRID}' stroke-width='1'/>")
        out.append(f"<text x='{left - 5}' y='{y + 3:.1f}' text-anchor='end' font-size='8.5' fill='{MUTED}'>{value:,}</text>")
    previous = None
    for i, (d, *_rest) in enumerate(points):
        month = (d.year, d.month)
        if previous is not None and month != previous and d.month % 3 == 1:   # Jan, Apr, Jul, Oct
            x, _ = xy(i, low)
            if left + 12 <= x <= W - right - 12:
                out.append(f"<text x='{x:.1f}' y='{H - 6}' text-anchor='middle' font-size='9' fill='{MUTED}'>{d:%b %y}</text>")
        previous = month
    out.append(f"<path d='{path(3)}' fill='none' stroke='{IG}' stroke-width='2' stroke-dasharray='4 4' stroke-linecap='round'/>")
    out.append(f"<path d='{path(1)}' fill='none' stroke='{FB}' stroke-width='2' stroke-linejoin='round' stroke-linecap='round'/>")
    out.append(f"<path d='{path(2)}' fill='none' stroke='{IG}' stroke-width='2' stroke-linejoin='round' stroke-linecap='round'/>")
    for index, colour in ((1, FB), (2, IG)):
        last = max((i for i, row in enumerate(points) if row[index] is not None), default=None)
        if last is None:
            continue
        x, y = xy(last, points[last][index])
        out.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='4' fill='{colour}' stroke='#fff' stroke-width='2'/>")
        out.append(f"<text x='{x + 8:.1f}' y='{y + 3.5:.1f}' font-size='10' font-weight='700' fill='{INK}'>{points[last][index]:,}</text>")
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

EXEC_CSS = """
:root{color-scheme:light;--blue:#3E3787;--yellow:#FAE226;--red:#CF3B41;--green:#60AC3F;--ink:#1a1a1a;--muted:#62606f;--soft:#f5f4fa;--line:#dddbe6}
*{box-sizing:border-box}
html{background:#e9e8f0}
body{margin:0;font-family:'Supreme',system-ui,-apple-system,'Segoe UI',sans-serif;color:var(--ink);line-height:1.35;font-size:10.2pt}
.bar{max-width:210mm;margin:0 auto;padding:12px 16px;display:flex;gap:12px;align-items:center;justify-content:space-between;font-size:.9rem}
.bar a{color:var(--blue);font-weight:700;text-decoration:none}
.bar button{font:inherit;font-weight:700;background:var(--blue);color:#fff;border:0;border-radius:999px;padding:.5rem 1rem;cursor:pointer}
.sheet{background:#fff;max-width:210mm;margin:0 auto 24px;box-shadow:0 8px 30px rgba(38,34,90,.18);overflow:hidden}
.head{background:linear-gradient(135deg,#3E3787 0%,#26225A 100%);color:#fff;padding:5mm 10mm 4.5mm;display:flex;align-items:center;gap:6mm;border-bottom:1.6mm solid var(--yellow);-webkit-print-color-adjust:exact;print-color-adjust:exact}
.head .crest{background:#fff;border-radius:3mm;padding:1.4mm 1.6mm;flex-shrink:0}
.head .crest img{height:15mm;display:block}
.head .kicker{color:var(--yellow);font-weight:700;font-size:7.6pt;letter-spacing:.12em;text-transform:uppercase}
.head h1{margin:.4mm 0 0;font-size:16pt;line-height:1.1;text-transform:uppercase;letter-spacing:.03em}
.head p{margin:1mm 0 0;font-size:8.6pt;opacity:.9}
.body{padding:4mm 10mm 3.5mm;display:grid;gap:2.8mm;grid-template-columns:minmax(0,1fr)}
.short>*,.two>*,.tiles>*,.top li>div{min-width:0}
h2{margin:0 0 1.6mm;font-size:8pt;text-transform:uppercase;letter-spacing:.08em;color:var(--blue)}
.short{display:grid;grid-template-columns:minmax(0,1.75fr) minmax(0,1fr);gap:4mm}
.short ol{margin:0;padding:0;list-style:none;display:grid;gap:1.7mm}
.short li{font-size:9pt;padding-left:3mm;border-left:1mm solid var(--blue)}
.rec{background:var(--yellow);border-radius:2.5mm;padding:3.2mm 3.6mm;font-size:9pt;align-self:start;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.rec h2{color:var(--ink)}
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:3mm}
.tile{background:var(--soft);border-radius:2.5mm;padding:2.2mm 3.2mm 2.4mm;border-top:1.1mm solid var(--blue);-webkit-print-color-adjust:exact;print-color-adjust:exact}
.tile h3{margin:0 0 1mm;font-size:7.2pt;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.tile .big{font-size:17pt;font-weight:700;line-height:1.05}
.tile .pair{display:flex;gap:5mm}
.tile .pair div{display:flex;flex-direction:column}
.tile .pair small{font-size:6.6pt;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:400}
.tile .sub{font-size:7.4pt;color:var(--muted);margin-top:.9mm;line-height:1.3}
.tile .unit{font-size:9pt;font-weight:400}
.up{color:#2f7a16;font-weight:700}.down{color:#b3262c;font-weight:700}.flat{color:var(--muted);font-weight:700}
.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4mm}
.panel{border:.3mm solid var(--line);border-radius:2.5mm;padding:2.6mm 3.2mm}
.panel svg{width:100%;height:auto;display:block;font-family:inherit}
.legend{display:flex;gap:4mm;font-size:7.6pt;color:var(--muted);margin:0 0 1mm;flex-wrap:wrap}
.legend i{display:inline-block;width:2.6mm;height:2.6mm;border-radius:.6mm;margin-right:1.2mm;vertical-align:-.3mm;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.legend i.dash{background:repeating-linear-gradient(90deg,var(--red) 0 1.1mm,transparent 1.1mm 1.9mm);height:.7mm;width:5mm;vertical-align:.7mm;border-radius:0}
.cap{font-size:7.2pt;color:var(--muted);margin:1mm 0 0}
.cov{display:grid;gap:1.25mm}
.cov .row{display:grid;grid-template-columns:31mm 1fr 6mm;gap:2mm;align-items:center;font-size:8.2pt}
.cov .track{height:3mm;background:var(--soft);border-radius:1.5mm;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.cov .fill{height:100%;background:var(--blue);border-radius:0 1.5mm 1.5mm 0;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.cov .n{text-align:right;font-weight:700;font-variant-numeric:tabular-nums}
.cov .row.zero{color:#b3262c;font-weight:700}
.top{margin:0;padding:0;list-style:none;display:grid;gap:1.8mm}
.top li{display:grid;grid-template-columns:5mm 1fr;gap:2mm;font-size:8.2pt}
.top .rank{background:var(--yellow);border-radius:50%;width:5mm;height:5mm;display:grid;place-items:center;font-weight:700;font-size:7.6pt;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.top .meta{color:var(--muted);font-size:7.3pt;text-transform:uppercase;letter-spacing:.04em}
.top .capt{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.top a{color:inherit;text-decoration:none}
.foot{border-top:.3mm solid var(--line);padding-top:2.4mm;font-size:7.1pt;color:var(--muted);display:grid;gap:1mm}
.foot b{color:var(--ink)}
.foot .sig{display:flex;justify-content:space-between;gap:4mm;margin-top:.6mm}
.foot .sig span:first-child{color:var(--blue);font-weight:700;letter-spacing:.08em}
@media (max-width:640px){body{font-size:11pt}.short,.two{grid-template-columns:minmax(0,1fr)}.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.head{padding:16px}.body{padding:14px}.head h1{font-size:15pt}.tile .big{font-size:15pt}.tile .pair{gap:3mm}.foot .sig{flex-direction:column;gap:1mm}}
/* the measuring copy must use the print layout whatever the screen width */
.measuring .short{grid-template-columns:minmax(0,1.75fr) minmax(0,1fr)}.measuring .two{grid-template-columns:repeat(2,minmax(0,1fr))}.measuring .tiles{grid-template-columns:repeat(3,minmax(0,1fr))}
.measuring{font-size:10.2pt}.measuring .head{padding:5mm 10mm 4.5mm}.measuring .body{padding:4mm 10mm 3.5mm}.measuring .head h1{font-size:16pt}.measuring .tile .big{font-size:17pt}.measuring .tile .pair{gap:5mm}.measuring .foot .sig{flex-direction:row}
@page{size:A4;margin:0}
@media print{html{background:#fff}body{font-size:10.2pt}.bar{display:none}.head{padding:5mm 10mm 4.5mm}.body{padding:4mm 10mm 3.5mm}.head h1{font-size:16pt}.tile .big{font-size:17pt}.tile .pair{gap:5mm}.foot .sig{flex-direction:row}.sheet{box-shadow:none;margin:0;max-width:none;width:210mm}
.short{grid-template-columns:minmax(0,1.75fr) minmax(0,1fr)}.two{grid-template-columns:repeat(2,minmax(0,1fr))}.tiles{grid-template-columns:repeat(3,minmax(0,1fr))}.sheet{zoom:var(--fit,1)}}
"""


def _delta(cur, prev, label):
    pct = rs.pct_change(cur, prev)
    if pct is None:
        return ""
    if pct == 0:
        return f"<span class='flat'>level</span> {label}"
    cls, arrow = ("up", "▲") if pct > 0 else ("down", "▼")
    return f"<span class='{cls}'>{arrow} {abs(pct)}%</span> {label}"


def _signed(n):
    return f"{'+' if n >= 0 else '−'}{abs(n):,}"


def _tile(title, big, sub, accent=None):
    style = f" style='border-top-color:{accent}'" if accent else ""
    return f"<div class='tile'{style}><h3>{title}</h3><div class='big'>{big}</div><div class='sub'>{sub}</div></div>"


def _pair(a, a_label, b, b_label):
    return (f"<div class='pair'><div>{a}<small>{a_label}</small></div>"
            f"<div>{b}<small>{b_label}</small></div></div>")


def tiles(f):
    days = f["days"]
    out = []
    moves = [x for x in (_delta(f["fb_views"], f["fb_views_prev"], f"on previous {days} days"),
                         _delta(f["fb_views"], f["fb_views_last_year"], "on last year")) if x]
    out.append(_tile("Views", rs.num(f["total_views"]),
                     f"Facebook {rs.num(f['fb_views'])} · Instagram {rs.num(f['ig_views'])}"
                     + (f"<br>Facebook {' · '.join(moves)}" if moves else ""), rs.BRAND["yellow"]))

    mult = [f"{m:.1f}×" for m in (f["fb_multiple"], f["ig_multiple"]) if m]
    sub = f"{' and '.join(mult)} the follower counts" if len(mult) == 2 else ""
    year = _delta(f["fb_reach"], f["fb_reach_last_year"], "on last year")
    if year:
        sub += ("<br>" if sub else "") + f"Facebook {year}"
    out.append(_tile("People reached in four weeks",
                     _pair(rs.num(f["fb_reach"]), "Facebook", rs.num(f["ig_reach"]), "Instagram"),
                     sub or "Not returned by Meta on the last run", rs.BRAND["blue"]))

    growth = []
    for platform in ("fb", "ig"):
        if f[f"{platform}_net"] is not None:
            growth.append(_signed(f[f"{platform}_net"]))
    since = []
    for platform, name in (("fb", "Facebook"), ("ig", "Instagram")):
        anchor, now = f[f"{platform}_anchor"], f[f"{platform}_followers"]
        if anchor and now:
            since.append(f"{_signed(round(100 * (now - anchor['count']) / anchor['count']))}%")
    out.append(_tile("Followers",
                     _pair(rs.num(f["fb_followers"]), "Facebook", rs.num(f["ig_followers"]), "Instagram"),
                     ((" and ".join(growth) + f" in {days} days") if len(growth) == 2 else "")
                     + (f"<br>{' and '.join(since)} since August 2025" if len(since) == 2 else ""), rs.BRAND["green"]))

    def pct(v):
        return "—" if v is None else f"{v}%"
    out.append(_tile("Views from non-followers",
                     _pair(pct(f["fb_nonfollower_share"]), "Facebook", pct(f["ig_nonfollower_share"]), "Instagram"),
                     "Views by people who don't follow yet", rs.BRAND["red"]))

    out.append(_tile("Likes, comments and shares", rs.num(f["interactions"]),
                     f"On posts published in the {days} days"
                     + (f"<br>{_delta(f['interactions'], f['interactions_prev'], f'on previous {days} days')}"
                        if f["interactions_prev"] else ""), rs.BRAND["green"]))

    out.append(_tile("Content published", f"{f['pieces']:,} <span class='unit'>pieces</span>",
                     f"{f['ig_posts']} Instagram posts · {f['fb_posts']} Facebook posts<br>"
                     "On both platforms counts once", rs.BRAND["yellow"]))
    return "".join(out)


def reach_panel(saved, today):
    monthly = saved.monthly_reach
    fb_m, ig_m = monthly.get("facebook") or {}, monthly.get("instagram") or {}
    this_month = f"{today:%Y-%m}"
    keys = sorted(k for k in set(fb_m) | set(ig_m) if k < this_month)[-6:]
    if not keys:
        return "<div class='panel'><h2>People reached each month</h2><p class='cap'>Builds up from the next run.</p></div>"
    labels = [f"{date(int(k[:4]), int(k[5:]), 1):%b}" for k in keys]
    fb_vals = [(fb_m.get(k) or {}).get("value") or 0 for k in keys]
    ig_vals = [(ig_m.get(k) or {}).get("value") or 0 for k in keys]
    note = "Different people who saw any club content in the four weeks to each month end (Facebook 28 days, Instagram 30)."
    first_ig = min(ig_m) if ig_m else None
    if first_ig and first_ig > keys[0]:
        note += f" Meta holds no Instagram figure before {date(int(first_ig[:4]), int(first_ig[5:]), 1):%B}."
    return (f"<div class='panel'><h2>People reached each month</h2>"
            f"<div class='legend'><span><i style='background:{FB}'></i>Facebook</span>"
            f"<span><i style='background:{IG}'></i>Instagram</span></div>"
            f"{svg_monthly_reach(labels, fb_vals, ig_vals)}<p class='cap'>{note}</p></div>")


def followers_panel(saved, snapshots, today):
    start = today - timedelta(days=365)
    days = rs.day_list(start, today)
    first_real = min(saved.ig_followers) if saved.ig_followers else None
    anchor_day = date.fromisoformat(rs.IG_START_FIGURE["date"])
    rows = []
    for d in days[::3] + [days[-1]]:
        key = d.isoformat()
        estimate = None
        if first_real:
            real_day = date.fromisoformat(first_real)
            if anchor_day <= d <= real_day:
                a, b = rs.IG_START_FIGURE["count"], saved.ig_followers[first_real]
                estimate = round(a + (b - a) * (d - anchor_day).days / ((real_day - anchor_day).days or 1))
        rows.append((d, saved.fb_followers(key), saved.ig_followers.get(key), estimate))
    # End on the newest counts, so the labels match the Followers tile above.
    fb_now, ig_now = rs.followers_now(snapshots, "facebook"), rs.followers_now(snapshots, "instagram")
    d, fb_last, ig_last, est_last = rows[-1]
    rows[-1] = (d, fb_now or fb_last, ig_now or ig_last, est_last)
    svg = svg_followers(rows)
    if not svg:
        return "<div class='panel'><h2>Followers, last 12 months</h2><p class='cap'>Builds up from the next run.</p></div>"
    note = ""
    if first_real:
        note = (f"Instagram before {date.fromisoformat(first_real):%-d %B} is an estimate: a straight line from the "
                f"{rs.IG_START_FIGURE['count']:,} recorded on 1 August 2025.")
    return (f"<div class='panel'><h2>Followers, last 12 months</h2>"
            f"<div class='legend'><span><i style='background:{FB}'></i>Facebook</span>"
            f"<span><i style='background:{IG}'></i>Instagram</span>"
            f"<span><i class='dash'></i>Instagram, estimated</span></div>{svg}<p class='cap'>{note}</p></div>")


def coverage_panel(f):
    peak = max(f["coverage"].values()) or 1
    rows = ""
    for section in rs.SECTIONS:
        n = f["coverage"][section]
        rows += (f"<div class='row{'' if n else ' zero'}'><span>{escape(section)}</span>"
                 f"<div class='track'><div class='fill' style='width:{100 * n / peak:.0f}%'></div></div>"
                 f"<span class='n'>{n}</span></div>")
    gaps = f["missing_period"]
    note = (f"Not featured in {f['days']} days: {_join(gaps)}." if gaps else
            f"Every section featured at least once in {f['days']} days.")
    return (f"<div class='panel'><h2>One Club, One Leam: who featured</h2><div class='cov'>{rows}</div>"
            f"<p class='cap'>Pieces of content per section, read from captions. {note}</p></div>")


def top_panel(f):
    items = ""
    for i, piece in enumerate(f["top"], 1):
        lead = rs.piece_lead(piece)
        caption = escape(" ".join((lead.get("caption") or "(no caption)").split())[:110])
        items += (f"<li><span class='rank'>{i}</span><div><div class='meta'>{rs.piece_date(piece):%-d %b} · "
                  f"{rs.PLATFORM_NAME[lead['platform']]} · {escape(rs.piece_section(piece)[0])} · "
                  f"{escape(rs.format_label(lead))}</div>"
                  f"<a href='{escape(lead.get('permalink') or '#', quote=True)}'><div class='capt'>{caption}</div></a>"
                  f"<div><b>{rs.post_reach(lead):,}</b> reached · <b>{rs.piece_views(piece):,}</b> views · "
                  f"<b>{rs.piece_interactions(piece):,}</b> interactions</div></div></li>")
    if not items:
        items = "<li><span></span><div>No posts in the period.</div></li>"
    return (f"<div class='panel'><h2>Furthest-reaching posts</h2><ol class='top'>{items}</ol>"
            "<p class='cap'>Reach is on the platform shown. Views and interactions include the copy on the other platform.</p></div>")


EXEC_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Leamington RFC · Social media summary for the Executive Committee</title>
<link rel="stylesheet" href="https://api.fontshare.com/v2/css?f[]=supreme@400,700&display=swap">
<style>%%CSS%%</style>
</head>
<body>
<div class="bar"><a href="index.html">← Full dashboard</a><button type="button" onclick="window.print()">Print or save as PDF</button></div>
<article class="sheet">
  <header class="head">
    <div class="crest"><img src="data:image/png;base64,%%CREST%%" alt="Leamington RFC centenary crest"></div>
    <div>
      <div class="kicker">Leamington RFC · 1926 to 2026</div>
      <h1>Social media summary</h1>
      <p>For the Executive Committee · %%RANGE%% (the last %%DAYS%% days) · Facebook and Instagram · figures from Meta, updated %%UPDATED%%</p>
    </div>
  </header>
  <div class="body">
    <section class="short">
      <div><h2>In short</h2><ol>%%SENTENCES%%</ol></div>
      <div class="rec"><h2>Recommended for the next fortnight</h2>%%RECOMMENDATION%%</div>
    </section>
    <section class="tiles">%%TILES%%</section>
    <section class="two">%%REACH%%%%FOLLOWERS%%</section>
    <section class="two">%%COVERAGE%%%%TOP%%</section>
    <footer class="foot">
      <div><b>Reach</b>: different people who saw any club content. <b>Views</b>: times it was seen, so one person can add several. <b>Interactions</b>: likes, comments, shares and saves. Reach is never added across Facebook and Instagram, because the same person may use both.</div>
      <div class="sig"><span>ONE CLUB, ONE LEAM</span><span>Full dashboard and glossary: %%LINK%%</span></div>
    </footer>
  </div>
</article>
<script>
/* Print safety net. The page is designed to fit one A4 sheet. If a long
   caption or an unusual font ever makes it taller, shrink it to fit rather
   than spilling onto a second page. The page works without this. */
(function () {
  function fit() {
    var sheet = document.querySelector('.sheet');
    var probe = sheet.cloneNode(true);
    probe.style.cssText = 'position:absolute;left:-9999px;top:0;width:210mm;max-width:none;visibility:hidden';
    probe.classList.add('measuring');
    document.body.appendChild(probe);
    var mm = probe.getBoundingClientRect().width / 210;
    var tall = probe.getBoundingClientRect().height / mm;
    document.body.removeChild(probe);
    document.documentElement.style.setProperty('--fit', tall > 295 ? (295 / tall).toFixed(3) : 1);
  }
  window.addEventListener('beforeprint', fit);
  if (document.fonts && document.fonts.ready) { document.fonts.ready.then(fit); } else { fit(); }
})();
</script>
</body>
</html>"""

DASHBOARD_URL = "symmo22.github.io/lrfc-dashboard"


def render_exec(account_snapshots, posts, insights, crest, today, history, updated):
    all_posts = list(posts.values())
    measured = [p for p in all_posts if rs.is_measured(p)]
    saved = rs.SavedHistory(history, account_snapshots, all_posts)
    P = rs.Period(EXEC_DAYS, insights, measured, today, all_posts)
    recent = rs.Period(RECENT_DAYS, insights, measured, today, all_posts)
    f = build_facts(P, recent, saved, account_snapshots, today)
    words = commentary(f)
    sentences = "".join(f"<li>{s}</li>" for s in words["sentences"]) or "<li>Not enough data yet.</li>"
    span = f"{f['start']:%-d %B} to {f['end']:%-d %B %Y}"
    return (EXEC_PAGE.replace("%%CSS%%", EXEC_CSS)
            .replace("%%CREST%%", crest)
            .replace("%%RANGE%%", span)
            .replace("%%DAYS%%", str(EXEC_DAYS))
            .replace("%%UPDATED%%", updated)
            .replace("%%SENTENCES%%", sentences)
            .replace("%%RECOMMENDATION%%", words["recommendation"])
            .replace("%%TILES%%", tiles(f))
            .replace("%%REACH%%", reach_panel(saved, today))
            .replace("%%FOLLOWERS%%", followers_panel(saved, account_snapshots, today))
            .replace("%%COVERAGE%%", coverage_panel(f))
            .replace("%%TOP%%", top_panel(f))
            .replace("%%LINK%%", DASHBOARD_URL))
