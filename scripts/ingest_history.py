#!/usr/bin/env python3
"""
Leamington RFC social dashboard - the dashboard's own saved history.

Why this exists: Instagram keeps account-level data for 90 days and
Facebook for 2 years. A 12-month view can't rely on asking Meta, so the
dashboard saves each day's figures itself. Every day this doesn't run, the
oldest Instagram day expires for good, which is why the first runs
backfill OLDEST FIRST: the days closest to expiring are saved first.

data/history.json
  daily.instagram["YYYY-MM-DD"]
      reach, accounts_engaged      unique that day (daily charts only: never add these up)
      views, total_interactions, likes, comments, shares, saves
      follows, unfollows, profile_links_taps
  daily.facebook["YYYY-MM-DD"]
      views, viewers (unique that day), follows, unfollows
      page_views (Page visits), page_follows (follower total, if Meta provides it)
      views_by_is_from_followers, views_by_is_from_ads (raw breakdowns, labelled in the renderer)
  rolling_reach.instagram["YYYY-MM-DD"]  unique accounts reached in the 30 days ending that day
  rolling_reach.facebook["YYYY-MM-DD"]   Meta's 28-day unique reach ending that day
      These are the figures the apps show; saving one per day gives a real
      reach trend line. Instagram can only be backfilled while the 30-day
      window is inside Meta's 90 days (about 60 days back); Facebook 2 years.
  monthly_reach.instagram["YYYY-MM"]  {"value", "days": 30}  30 days to month end
  monthly_reach.facebook["YYYY-MM"]   {"value", "days": 28}  Meta's 28-day figure at month end

Monthly reach is stored once per completed month and never refetched.
Instagram's API accepts at most 30 days per request, so a 31-day month is
measured as the 30 days to month end; Facebook's unique reach only exists
as 7- or 28-day figures. Both are labelled on the dashboard.

Idempotent: re-running a day overwrites it. The last few days are always
re-fetched because Meta finalises figures a day or two late. A day where
any part failed is marked incomplete and retried next run.

The workflow runs this step with continue-on-error, so it can never block
the core daily snapshot.
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from graph import Notes, env, get_page_token, graph_get  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HISTORY_FILE = DATA_DIR / "history.json"

IG_RETENTION_DAYS = 89       # one day inside Meta's 90, to avoid edge-of-window errors
FB_RETENTION_DAYS = 729      # one day inside Meta's 2 years
IG_BACKFILL_PER_RUN = 30     # days per run while backfilling; keeps calls modest
IG_ROLLING_PER_RUN = 30      # rolling-reach days per run while backfilling
REFETCH_RECENT_DAYS = 3      # Meta finalises figures late
FB_MONTHS_BACK = 24

IG_DAY_METRICS = ["reach", "accounts_engaged", "views", "total_interactions",
                  "likes", "comments", "shares", "saves"]
FB_DAILY_METRICS = {"views": "page_media_view", "viewers": "page_total_media_view_unique",
                    "follows": "page_daily_follows_unique", "unfollows": "page_daily_unfollows_unique",
                    "page_views": "page_views_total", "page_follows": "page_follows"}
# Stored raw (Meta's own keys) and labelled in the renderer: the key names
# for these breakdowns aren't documented clearly enough to hard-code here.
FB_BREAKDOWNS = {"views_by_is_from_followers": ("page_media_view", "is_from_followers"),
                 "views_by_is_from_ads": ("page_media_view", "is_from_ads")}


def midnight_ts(day):
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def chunks(since, until, max_days):
    start = since
    while start < until:
        end = min(start + timedelta(days=max_days), until)
        yield start, end
        start = end


def completed_months(today, count):
    """(key, first_day, last_day) for the `count` most recent completed months."""
    months = []
    first_of_this_month = today.replace(day=1)
    end = first_of_this_month - timedelta(days=1)
    for _ in range(count):
        start = end.replace(day=1)
        months.append((start.strftime("%Y-%m"), start, end))
        end = start - timedelta(days=1)
    return months


def fb_value_day(end_time):
    """Facebook stamps a daily value with the end of that day in Pacific time
    (07:00 UTC the next morning), so the value belongs to the day before."""
    return date.fromisoformat(end_time[:10]) - timedelta(days=1)



# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

def ig_total(ig_id, token, app_secret, metrics, since, until, breakdown=None):
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


def ig_day(ig_id, token, app_secret, day, notes):
    """One day's Instagram figures. Returns None if the core call fails, so
    nothing partial is written for that day."""
    nxt = day + timedelta(days=1)
    core = notes.attempt(f"Instagram {day} totals",
                         lambda: ig_total(ig_id, token, app_secret, IG_DAY_METRICS, day, nxt))
    if core is None:
        return None
    record = dict(core)
    complete = True
    follows = notes.attempt(f"Instagram {day} follows",
                            lambda: ig_total(ig_id, token, app_secret, ["follows_and_unfollows"], day, nxt,
                                             "follow_type"))
    if follows is not None:
        split = follows.get("follows_and_unfollows") or {}
        record["follows"] = split.get("FOLLOWER", 0)
        record["unfollows"] = split.get("NON_FOLLOWER", 0)
    else:
        complete = False
    taps = notes.attempt(f"Instagram {day} link taps",
                         lambda: ig_total(ig_id, token, app_secret, ["profile_links_taps"], day, nxt))
    if taps is not None:
        record["profile_links_taps"] = taps.get("profile_links_taps")
    else:
        complete = False
    if not complete:
        record["_incomplete"] = True
    return record


# ---------------------------------------------------------------------------
# Facebook
# ---------------------------------------------------------------------------

def fb_series(page_id, page_token, app_secret, metric, period, since, until):
    """{date: value} for a metric over any range, 30 days per request."""
    out = {}
    for a, b in chunks(since, until, 30):
        result = graph_get(f"{page_id}/insights", page_token, app_secret, {
            "metric": metric, "period": period, "since": midnight_ts(a), "until": midnight_ts(b)})
        for row in result.get("data", []):
            if row.get("period") != period:
                continue
            for v in row.get("values", []):
                if isinstance(v.get("value"), (int, float)) and v.get("end_time"):
                    out[fb_value_day(v["end_time"]).isoformat()] = v["value"]
    return out


def fb_breakdown_series(page_id, page_token, app_secret, metric, breakdown, since, until):
    """{date: {breakdown_value: n}}. If Meta's response isn't the expected
    shape, raises with a sample of it so the next version can adapt: the
    note lands in history.json, which is readable in the public repo."""
    out = {}
    for a, b in chunks(since, until, 30):
        result = graph_get(f"{page_id}/insights", page_token, app_secret, {
            "metric": metric, "period": "day", "breakdown": breakdown,
            "since": midnight_ts(a), "until": midnight_ts(b)})
        for row in result.get("data", []):
            if row.get("period") not in (None, "day"):
                continue
            for v in row.get("values", []):
                value, end = v.get("value"), v.get("end_time")
                if not end:
                    continue
                if not isinstance(value, dict):
                    raise ValueError("unexpected breakdown shape: " + json.dumps(row)[:400])
                out.setdefault(fb_value_day(end).isoformat(), {}).update(
                    {str(k): n for k, n in value.items() if isinstance(n, (int, float))})
    return out


# ---------------------------------------------------------------------------

def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {}


def main():
    token = env("META_SYSTEM_USER_TOKEN")
    app_secret = env("META_APP_SECRET")
    page_id = env("META_PAGE_ID")
    ig_id = env("META_IG_USER_ID")
    today = datetime.now(timezone.utc).date()
    notes = Notes()

    history = load_history()
    daily = history.setdefault("daily", {})
    ig_daily = daily.setdefault("instagram", {})
    fb_daily = daily.setdefault("facebook", {})
    monthly = history.setdefault("monthly_reach", {})
    ig_monthly = monthly.setdefault("instagram", {})
    fb_monthly = monthly.setdefault("facebook", {})
    fb_done = history.setdefault("facebook_backfilled", {})

    # --- Instagram daily: oldest missing days first, then the last few days
    oldest = today - timedelta(days=IG_RETENTION_DAYS)
    all_days = [oldest + timedelta(days=i) for i in range((today - oldest).days)]
    recent = all_days[-REFETCH_RECENT_DAYS:]
    backlog = [d for d in all_days if d not in recent
               and (d.isoformat() not in ig_daily or ig_daily[d.isoformat()].get("_incomplete"))]
    todo = backlog[:IG_BACKFILL_PER_RUN] + recent
    print(f"Instagram daily history: {len(backlog)} day(s) to backfill, doing {min(len(backlog), IG_BACKFILL_PER_RUN)} "
          f"oldest first, plus the last {len(recent)}.")
    saved = 0
    for day in todo:
        record = ig_day(ig_id, token, app_secret, day, notes)
        if record is not None:
            ig_daily[day.isoformat()] = record
            saved += 1
    print(f"  Saved {saved} Instagram day(s).")

    # --- Instagram monthly reach: completed months still inside retention
    for key, first, last in completed_months(today, 4):
        since = last + timedelta(days=1) - timedelta(days=30)
        if key in ig_monthly or since < oldest:
            continue
        got = notes.attempt(f"Instagram {key} reach",
                            lambda s=since, u=last + timedelta(days=1): ig_total(ig_id, token, app_secret,
                                                                                 ["reach"], s, u))
        if got and got.get("reach") is not None:
            ig_monthly[key] = {"value": got["reach"], "days": 30,
                               "from": since.isoformat(), "to": last.isoformat()}
            print(f"  Instagram {key} reach saved: {got['reach']:,}")

    # --- Instagram rolling 30-day reach, one value per day
    rolling = history.setdefault("rolling_reach", {})
    ig_rolling = rolling.setdefault("instagram", {})
    fb_rolling = rolling.setdefault("facebook", {})
    first_end = oldest + timedelta(days=29)   # earliest 30-day window still inside retention
    ends = [first_end + timedelta(days=i) for i in range((today - first_end).days)]
    recent_ends = ends[-REFETCH_RECENT_DAYS:]
    rolling_backlog = [d for d in ends if d not in recent_ends and d.isoformat() not in ig_rolling]
    for end in rolling_backlog[:IG_ROLLING_PER_RUN] + recent_ends:
        got = notes.attempt(f"Instagram {end} 30-day reach",
                            lambda e=end: ig_total(ig_id, token, app_secret, ["reach"],
                                                   e - timedelta(days=29), e + timedelta(days=1)))
        if got and got.get("reach") is not None:
            ig_rolling[end.isoformat()] = got["reach"]
    print(f"  Instagram rolling reach: {len(ig_rolling)} day(s) saved, {max(0, len(rolling_backlog) - IG_ROLLING_PER_RUN)} to go.")

    # --- Facebook
    page_token = notes.attempt("Facebook page token", lambda: get_page_token(page_id, token, app_secret))
    if page_token:
        for field, metric in FB_DAILY_METRICS.items():
            full = not fb_done.get(metric)
            since = today - timedelta(days=FB_RETENTION_DAYS if full else REFETCH_RECENT_DAYS + 2)
            series = notes.attempt(f"Facebook daily {field}",
                                   lambda m=metric, s=since: fb_series(page_id, page_token, app_secret, m, "day", s, today))
            if series is None:
                continue
            for day, value in series.items():
                fb_daily.setdefault(day, {})[field] = value
            if full:
                fb_done[metric] = True
            print(f"  Facebook {field}: {len(series)} day(s) {'backfilled' if full else 'refreshed'}.")

        full = not fb_done.get("rolling_reach")
        since = today - timedelta(days=FB_RETENTION_DAYS if full else REFETCH_RECENT_DAYS + 2)
        series = notes.attempt("Facebook rolling 28-day reach",
                               lambda s=since: fb_series(page_id, page_token, app_secret,
                                                         "page_total_media_view_unique", "days_28", s, today))
        if series is not None:
            fb_rolling.update(series)
            fb_done["rolling_reach"] = True
            print(f"  Facebook rolling reach: {len(series)} day(s) {'backfilled' if full else 'refreshed'}.")

        for field, (metric, breakdown) in FB_BREAKDOWNS.items():
            full = not fb_done.get(field)
            since = today - timedelta(days=FB_RETENTION_DAYS if full else REFETCH_RECENT_DAYS + 2)
            series = notes.attempt(f"Facebook daily {field}",
                                   lambda m=metric, b=breakdown, s=since: fb_breakdown_series(
                                       page_id, page_token, app_secret, m, b, s, today))
            if series is None:
                continue
            for day, split in series.items():
                fb_daily.setdefault(day, {})[field] = split
            fb_done[field] = True
            print(f"  Facebook {field}: {len(series)} day(s) {'backfilled' if full else 'refreshed'}.")

        for key, first, last in completed_months(today, FB_MONTHS_BACK):
            if key in fb_monthly:
                continue
            series = notes.attempt(
                f"Facebook {key} reach",
                lambda l=last: fb_series(page_id, page_token, app_secret, "page_total_media_view_unique", "days_28",
                                         l - timedelta(days=1), l + timedelta(days=2)))
            value = (series or {}).get(last.isoformat())
            if value is not None:
                fb_monthly[key] = {"value": value, "days": 28, "to": last.isoformat()}

    history["updated"] = datetime.now(timezone.utc).isoformat()
    history["last_run_notes"] = notes.items[:50]
    history["last_run_note_count"] = len(notes.items)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=1, sort_keys=True), encoding="utf-8")

    ig_days = sorted(k for k, v in ig_daily.items() if not v.get("_incomplete"))
    fb_days = sorted(fb_daily)
    print(f"Instagram history: {len(ig_days)} complete day(s)"
          + (f", from {ig_days[0]}" if ig_days else ""))
    print(f"Facebook history: {len(fb_days)} day(s)" + (f", from {fb_days[0]}" if fb_days else ""))
    print(f"Monthly reach saved: Instagram {sorted(ig_monthly)}, Facebook {len(fb_monthly)} month(s)")
    print(f"Rolling reach saved: Instagram {len(ig_rolling)} day(s), Facebook {len(fb_rolling)} day(s)")
    print(f"Done with {notes.summary()}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
