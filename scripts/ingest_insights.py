#!/usr/bin/env python3
"""
Leamington RFC social dashboard - rolling-window and audience insights.

For each dashboard window (30 and 90 days) fetches account totals for
that window and the one before it, plus Instagram breakdowns (followers vs
non-followers, content type, follows and unfollows). Also fetches
Instagram follower demographics once (they aren't window-based).

Writes data/insights.json, overwritten each run: every figure is a rolling
window Meta can recompute, so there's nothing to accumulate.

Meta's limits, handled explicitly rather than papered over:
  - Instagram accepts at most 30 days per request. Longer windows are
    fetched as 30-day chunks and summed, but only for counts that can be
    summed (views, likes, interactions...). Reach counts unique accounts,
    so it can't be summed across chunks and isn't given beyond 30 days.
  - Instagram keeps account-level data for 90 days, so there is no
    "previous 90 days" to compare against.
  - Facebook reach (unique people) only exists as Meta's own 28-day
    figure. Facebook views are summed from daily values.
  - If any chunk of a summed figure fails, the whole figure is dropped,
    never shown as a partial total.

Every figure is fetched on its own; failures become notes, never a crash.
The workflow also runs this step with continue-on-error.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from graph import env, get_page_token, graph_get  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INSIGHTS_FILE = DATA_DIR / "insights.json"
WINDOWS = [30, 90]  # 12 months comes from saved history (ingest_history.py)
IG_MAX_RANGE = 30
IG_RETENTION_DAYS = 90
FB_REACH_PERIOD = {30: ("days_28", 28)}

IG_REACH_GROUP = ["reach", "accounts_engaged"]
IG_SUM_GROUPS = [["views", "total_interactions"], ["likes", "comments", "shares", "saves"],
                 ["profile_links_taps"]]


def midnight_ts(day):
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def chunks(since, until, max_days):
    start = since
    while start < until:
        end = min(start + timedelta(days=max_days), until)
        yield start, end
        start = end


class Notes:
    def __init__(self):
        self.items = []

    def attempt(self, label, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - optional figure, never fatal
            self.items.append(f"{label}: {exc}")
            print(f"  WARN {label}: {exc}", file=sys.stderr)
            return None


def _add(total, value):
    if isinstance(value, dict):
        total = dict(total or {})
        for k, v in value.items():
            total[k] = (total.get(k) or 0) + (v or 0)
        return total
    return (total or 0) + (value or 0)


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

def ig_totals(ig_id, token, app_secret, metrics, since, until, breakdown=None):
    params = {"metric": ",".join(metrics), "period": "day", "metric_type": "total_value",
              "since": midnight_ts(since), "until": midnight_ts(until)}
    if breakdown:
        params["breakdown"] = breakdown
    result = graph_get(f"{ig_id}/insights", token, app_secret, params)
    out = {}
    for row in result.get("data", []):
        total = row.get("total_value") or {}
        if breakdown:
            results = (total.get("breakdowns") or [{}])[0].get("results", [])
            out[row["name"]] = {"/".join(r.get("dimension_values", [])): r.get("value") for r in results}
        else:
            out[row["name"]] = total.get("value")
    if not out:
        raise ValueError("no data returned")
    return out


def ig_summed(ig_id, token, app_secret, metrics, since, until, breakdown=None):
    """Sum a summable metric over any length of window, 30 days at a time.
    Any failing chunk raises, so a partial total is never returned."""
    totals = {}
    for a, b in chunks(since, until, IG_MAX_RANGE):
        for name, value in ig_totals(ig_id, token, app_secret, metrics, a, b, breakdown).items():
            totals[name] = _add(totals.get(name), value)
    return totals


def ig_demographics(ig_id, token, app_secret, breakdown):
    last_error = None
    for timeframe in ("this_month", "this_week"):
        try:
            result = graph_get(f"{ig_id}/insights", token, app_secret, {
                "metric": "follower_demographics", "period": "lifetime", "metric_type": "total_value",
                "timeframe": timeframe, "breakdown": breakdown})
            data = result.get("data") or []
            results = ((data[0].get("total_value") or {}).get("breakdowns") or [{}])[0].get("results", []) if data else []
            if not results:
                raise ValueError("empty breakdown")
            return {"timeframe": timeframe,
                    "values": {"/".join(r.get("dimension_values", [])): r.get("value") for r in results}}
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise last_error


# ---------------------------------------------------------------------------
# Facebook
# ---------------------------------------------------------------------------

def fb_daily_sum(page_id, page_token, app_secret, metric, since, until):
    total, count = 0, 0
    for a, b in chunks(since, until, 30):
        result = graph_get(f"{page_id}/insights", page_token, app_secret, {
            "metric": metric, "period": "day", "since": midnight_ts(a), "until": midnight_ts(b)})
        for row in result.get("data", []):
            if row.get("period") != "day":
                continue
            for value in row.get("values", []):
                if isinstance(value.get("value"), (int, float)):
                    total += value["value"]
                    count += 1
    if count == 0:
        raise ValueError("no daily values returned")
    return total


def fb_rolling_latest(page_id, page_token, app_secret, metric, period, end):
    result = graph_get(f"{page_id}/insights", page_token, app_secret, {
        "metric": metric, "period": period,
        "since": midnight_ts(end - timedelta(days=2)), "until": midnight_ts(end)})
    for row in result.get("data", []):
        if row.get("period") == period:
            values = [v.get("value") for v in row.get("values", []) if isinstance(v.get("value"), (int, float))]
            if values:
                return values[-1]
    raise ValueError(f"no {period} values returned")


# ---------------------------------------------------------------------------

def main():
    token = env("META_SYSTEM_USER_TOKEN")
    app_secret = env("META_APP_SECRET")
    page_id = env("META_PAGE_ID")
    ig_id = env("META_IG_USER_ID")
    today = datetime.now(timezone.utc).date()
    notes = Notes()
    page_token = notes.attempt("Facebook page token", lambda: get_page_token(page_id, token, app_secret))

    windows = {}
    for days in WINDOWS:
        print(f"{days}-day window...")
        ranges = {"current": (today - timedelta(days=days), today),
                  "prior": (today - timedelta(days=2 * days), today - timedelta(days=days))}
        ig = {"current": {}, "prior": {}, "unavailable": {}}
        fb = {"current": {}, "prior": {}, "reach_days": FB_REACH_PERIOD.get(days, (None, None))[1]}

        for label, (since, until) in ranges.items():
            if (today - since).days > IG_RETENTION_DAYS:
                ig["unavailable"][label] = "Instagram keeps account data for 90 days"
                continue
            if days <= IG_MAX_RANGE:
                got = notes.attempt(f"Instagram {days}d {label} reach",
                                    lambda s=since, u=until: ig_totals(ig_id, token, app_secret, IG_REACH_GROUP, s, u))
                if got:
                    ig[label].update(got)
            for group in IG_SUM_GROUPS:
                got = notes.attempt(f"Instagram {days}d {label} {'/'.join(group)}",
                                    lambda g=group, s=since, u=until: ig_summed(ig_id, token, app_secret, g, s, u))
                if got:
                    ig[label].update(got)

        since, until = ranges["current"]
        for key, metric, breakdown in (("views_by_follower_type", "views", "follow_type"),
                                       ("views_by_product_type", "views", "media_product_type"),
                                       ("follows", "follows_and_unfollows", "follow_type")):
            got = notes.attempt(f"Instagram {days}d {metric} by {breakdown}",
                                lambda m=metric, b=breakdown: ig_summed(ig_id, token, app_secret, [m], since, until, b))
            ig[key] = (got or {}).get(metric)

        if page_token:
            for label, (s, u) in ranges.items():
                views = notes.attempt(f"Facebook {days}d {label} views",
                                      lambda s=s, u=u: fb_daily_sum(page_id, page_token, app_secret, "page_media_view", s, u))
                if views is not None:
                    fb[label]["views"] = views
                period = FB_REACH_PERIOD.get(days)
                if period:
                    reach = notes.attempt(f"Facebook {days}d {label} reach",
                                          lambda u=u, p=period[0]: fb_rolling_latest(
                                              page_id, page_token, app_secret, "page_total_media_view_unique", p, u))
                    if reach is not None:
                        fb[label]["reach"] = reach

        windows[str(days)] = {"range": {k: [a.isoformat(), b.isoformat()] for k, (a, b) in ranges.items()},
                              "instagram": ig, "facebook": fb}
        print(f"  Instagram {windows[str(days)]['instagram']['current']}")
        print(f"  Facebook {windows[str(days)]['facebook']['current']}")

    print("Instagram audience...")
    demographics = {}
    for breakdown in ("age", "gender", "city"):
        got = notes.attempt(f"Instagram follower {breakdown}",
                            lambda b=breakdown: ig_demographics(ig_id, token, app_secret, b))
        if got:
            demographics[breakdown] = got

    output = {"generated_at": datetime.now(timezone.utc).isoformat(), "windows": windows,
              "demographics": demographics, "notes": notes.items}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INSIGHTS_FILE.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Done with {len(notes.items)} note(s).")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
