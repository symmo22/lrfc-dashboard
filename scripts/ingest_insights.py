#!/usr/bin/env python3
"""
Leamington RFC social dashboard - rolling-window and audience insights.

Fetches, for both platforms:
  - account totals for the last 30 days and the 30 days before that
    (so every headline figure has a like-for-like comparison from day one)
  - Instagram views split by followers vs non-followers
  - Instagram views split by content type (posts, Reels, Stories...)
  - Instagram follows and unfollows
  - Instagram follower demographics (age, gender, city)

Writes the latest set to data/insights.json, overwritten each run. Every
figure here is a rolling window Meta can recompute on demand, so there is
nothing to accumulate.

Robustness: every metric is fetched on its own and any failure is recorded
as a note rather than stopping the run. The workflow also runs this step
with continue-on-error, so nothing here can block the core daily snapshot
in ingest.py and ingest_posts.py.

Windows: Instagram uses exactly 30 days to match what the Instagram app
shows. Facebook views are summed from daily values over the same 30 days
(views can be summed). Facebook reach cannot be summed across days because
it counts unique people, so it uses Meta's own 28-day rolling figure and is
labelled as 28 days on the dashboard.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from graph import env, get_page_token, graph_get  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INSIGHTS_FILE = DATA_DIR / "insights.json"
WINDOW_DAYS = 30


def midnight_ts(day):
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


class Notes:
    """Collects failures from optional fetches instead of raising."""

    def __init__(self):
        self.items = []

    def attempt(self, label, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - optional panel, never fatal
            message = f"{label}: {exc}"
            self.items.append(message)
            print(f"  WARN {message}", file=sys.stderr)
            return None


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

def ig_totals(ig_id, token, app_secret, metrics, since, until, breakdown=None):
    params = {
        "metric": ",".join(metrics),
        "period": "day",
        "metric_type": "total_value",
        "since": midnight_ts(since),
        "until": midnight_ts(until),
    }
    if breakdown:
        params["breakdown"] = breakdown
    result = graph_get(f"{ig_id}/insights", token, app_secret, params)
    out = {}
    for row in result.get("data", []):
        total = row.get("total_value") or {}
        if breakdown:
            breakdowns = total.get("breakdowns") or [{}]
            results = breakdowns[0].get("results", [])
            out[row["name"]] = {
                "/".join(r.get("dimension_values", [])): r.get("value") for r in results
            }
        else:
            out[row["name"]] = total.get("value")
    if not out:
        raise ValueError("no data returned")
    return out


def ig_demographics(ig_id, token, app_secret, breakdown):
    last_error = None
    for timeframe in ("this_month", "this_week"):
        try:
            result = graph_get(
                f"{ig_id}/insights",
                token,
                app_secret,
                {
                    "metric": "follower_demographics",
                    "period": "lifetime",
                    "metric_type": "total_value",
                    "timeframe": timeframe,
                    "breakdown": breakdown,
                },
            )
            data = result.get("data") or []
            if not data:
                raise ValueError("no data returned")
            breakdowns = (data[0].get("total_value") or {}).get("breakdowns") or [{}]
            results = breakdowns[0].get("results", [])
            if not results:
                raise ValueError("empty breakdown")
            return {
                "timeframe": timeframe,
                "values": {
                    "/".join(r.get("dimension_values", [])): r.get("value") for r in results
                },
            }
        except Exception as exc:  # noqa: BLE001 - try the next timeframe
            last_error = exc
    raise last_error


# ---------------------------------------------------------------------------
# Facebook
# ---------------------------------------------------------------------------

def fb_daily_sum(page_id, page_token, app_secret, metric, since, until):
    result = graph_get(
        f"{page_id}/insights",
        page_token,
        app_secret,
        {"metric": metric, "period": "day", "since": midnight_ts(since), "until": midnight_ts(until)},
    )
    total, count = 0, 0
    for row in result.get("data", []):
        if row.get("period") != "day":
            continue
        for value in row.get("values", []):
            v = value.get("value")
            if isinstance(v, (int, float)):
                total += v
                count += 1
    if count == 0:
        raise ValueError("no daily values returned")
    return total


def fb_rolling_latest(page_id, page_token, app_secret, metric, period, end):
    result = graph_get(
        f"{page_id}/insights",
        page_token,
        app_secret,
        {
            "metric": metric,
            "period": period,
            "since": midnight_ts(end - timedelta(days=2)),
            "until": midnight_ts(end),
        },
    )
    for row in result.get("data", []):
        if row.get("period") != period:
            continue
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
    cur_since, cur_until = today - timedelta(days=WINDOW_DAYS), today
    prev_since, prev_until = cur_since - timedelta(days=WINDOW_DAYS), cur_since
    windows = (("current", cur_since, cur_until), ("prior", prev_since, prev_until))

    notes = Notes()

    print("Instagram 30-day totals...")
    ig = {"current": {}, "prior": {}}
    for label, since, until in windows:
        for group, metrics in (
            ("reach and views", ["reach", "views", "accounts_engaged", "total_interactions"]),
            ("engagement", ["likes", "comments", "shares", "saves"]),
        ):
            got = notes.attempt(
                f"Instagram {label} {group}",
                lambda m=metrics, s=since, u=until: ig_totals(ig_id, token, app_secret, m, s, u),
            )
            if got:
                ig[label].update(got)

    print("Instagram breakdowns...")
    by_follower = notes.attempt(
        "Instagram views by follower type",
        lambda: ig_totals(ig_id, token, app_secret, ["views"], cur_since, cur_until, "follower_type"),
    )
    ig["views_by_follower_type"] = (by_follower or {}).get("views")
    by_product = notes.attempt(
        "Instagram views by content type",
        lambda: ig_totals(ig_id, token, app_secret, ["views"], cur_since, cur_until, "media_product_type"),
    )
    ig["views_by_product_type"] = (by_product or {}).get("views")
    follows = notes.attempt(
        "Instagram follows and unfollows",
        lambda: ig_totals(
            ig_id, token, app_secret, ["follows_and_unfollows"], cur_since, cur_until, "follow_type"
        ),
    )
    ig["follows"] = (follows or {}).get("follows_and_unfollows")

    print("Instagram audience...")
    demographics = {}
    for breakdown in ("age", "gender", "city"):
        got = notes.attempt(
            f"Instagram follower {breakdown}",
            lambda b=breakdown: ig_demographics(ig_id, token, app_secret, b),
        )
        if got:
            demographics[breakdown] = got
    ig["demographics"] = demographics

    print("Facebook 30-day totals...")
    fb = {"current": {}, "prior": {}}
    page_token = notes.attempt("Facebook page token", lambda: get_page_token(page_id, token, app_secret))
    if page_token:
        for label, since, until in windows:
            views = notes.attempt(
                f"Facebook {label} views",
                lambda s=since, u=until: fb_daily_sum(page_id, page_token, app_secret, "page_media_view", s, u),
            )
            if views is not None:
                fb[label]["views"] = views
            reach = notes.attempt(
                f"Facebook {label} reach",
                lambda u=until: fb_rolling_latest(
                    page_id, page_token, app_secret, "page_total_media_view_unique", "days_28", u
                ),
            )
            if reach is not None:
                fb[label]["reach_28d"] = reach

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": WINDOW_DAYS,
        "window": {"current": [cur_since.isoformat(), cur_until.isoformat()],
                   "prior": [prev_since.isoformat(), prev_until.isoformat()]},
        "instagram": ig,
        "facebook": fb,
        "notes": notes.items,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INSIGHTS_FILE.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")

    print(f"  Instagram current: {ig['current']}")
    print(f"  Facebook current: {fb['current']}")
    print(f"Done with {len(notes.items)} note(s).")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
