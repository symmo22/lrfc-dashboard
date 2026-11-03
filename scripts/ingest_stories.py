#!/usr/bin/env python3
"""
Leamington RFC social dashboard: Instagram Stories capture.

Meta keeps a Story's figures for 24 hours and then deletes them, so the
daily run can't measure Stories properly: a Story posted just after it
runs has gone before the next one. This script runs every hour from its
own workflow (.github/workflows/stories.yml). Each run lists the Stories
that are live now and records their figures. A Story's record keeps the
figures from its last check before it expired, so the final numbers can
be up to an hour or so short of Meta's true lifetime total.

What it records per Story: when it went up, photo or video, reach, views,
replies, shares, total interactions, follows, profile visits, link clicks,
and how people moved through it (taps forward and back, swipes to the
next account's Story, and exits). Nothing else: no image or video is
stored, because the repo is public and Stories often show junior players.

Facebook Stories are not covered. Meta's API gives no figures for them,
and even listing them needs a publishing permission this app doesn't hold.

Required environment variables: META_SYSTEM_USER_TOKEN, META_APP_SECRET,
META_IG_USER_ID. Safe to run as often as you like: records are keyed by
Story ID and only ever updated.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from graph import AuthError, BlockedError, check_token, env, graph_get, graph_get_url, is_rate_limited  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STORIES_FILE = DATA_DIR / "stories.json"

# Checked against Meta's IG Media Insights reference on 2026-11-03.
# 'impressions' is deprecated for media created after July 2024.
METRICS = ["reach", "views", "replies", "shares", "total_interactions",
           "follows", "profile_visits", "link_clicks"]
NAVIGATION_BREAKDOWN = "story_navigation_action_type"


def is_too_few_viewers(exc):
    """Meta refuses any Story metric under 5 with error #10 ('Not enough
    viewers'). That is a small number, not a failure."""
    text = str(exc)
    return "(#10)" in text or "Not enough viewers" in text or "error (10)" in text


def value_of(row):
    values = row.get("values") or []
    if values:
        return values[-1].get("value")
    return (row.get("total_value") or {}).get("value")


def fetch_metrics(story_id, token, app_secret):
    """All metrics in one call; if Meta refuses because one of them is
    under 5, ask for each separately so the others are still recorded.
    Returns (metrics, below_five)."""
    try:
        result = graph_get(f"{story_id}/insights", token, app_secret, {"metric": ",".join(METRICS)})
        return {row["name"]: value_of(row) for row in result.get("data", [])}, []
    except (AuthError, BlockedError):
        raise
    except Exception as exc:  # noqa: BLE001
        if not is_too_few_viewers(exc):
            raise
    metrics, below = {}, []
    for metric in METRICS:
        try:
            result = graph_get(f"{story_id}/insights", token, app_secret, {"metric": metric})
            for row in result.get("data", []):
                metrics[row["name"]] = value_of(row)
        except (AuthError, BlockedError):
            raise
        except Exception as exc:  # noqa: BLE001
            if not is_too_few_viewers(exc):
                raise
            below.append(metric)
    return metrics, below


def fetch_navigation(story_id, token, app_secret):
    """Taps forward, taps back, swipes to the next account and exits."""
    try:
        result = graph_get(f"{story_id}/insights", token, app_secret,
                           {"metric": "navigation", "breakdown": NAVIGATION_BREAKDOWN})
    except (AuthError, BlockedError):
        raise
    except Exception as exc:  # noqa: BLE001
        if is_too_few_viewers(exc):
            return None
        raise
    out = {}
    for row in result.get("data", []):
        for breakdown in (row.get("total_value") or {}).get("breakdowns", []):
            for r in breakdown.get("results", []):
                key = str((r.get("dimension_values") or ["unknown"])[0]).lower()
                out[key] = out.get(key, 0) + (r.get("value") or 0)
    return out


def list_live_stories(ig_id, token, app_secret):
    result = graph_get(f"{ig_id}/stories", token, app_secret,
                       {"fields": "id,media_type,media_product_type,timestamp,permalink", "limit": 50})
    items = []
    while True:
        items += result.get("data", [])
        nxt = (result.get("paging") or {}).get("next")
        if not nxt:
            return items
        result = graph_get_url(nxt)


def load():
    if STORIES_FILE.exists():
        return json.loads(STORIES_FILE.read_text(encoding="utf-8"))
    return {}


def save(stories):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STORIES_FILE.write_text(json.dumps(stories, indent=2, sort_keys=True), encoding="utf-8")


def upsert(stories, item, now, metrics, below, navigation):
    """Newer figures replace older ones. A metric Meta withheld this time
    (under 5) keeps its last known value rather than being blanked."""
    key = item["id"]
    old = stories.get(key, {})
    posted = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
    merged = dict(old.get("metrics") or {})
    merged.update({k: v for k, v in metrics.items() if v is not None})
    stories[key] = {
        "id": key,
        "published_at": item["timestamp"],
        "media_type": item.get("media_type"),
        "permalink": item.get("permalink"),
        "first_seen": old.get("first_seen") or now.isoformat(timespec="minutes"),
        "last_checked": now.isoformat(timespec="minutes"),
        "age_hours_at_last_check": round((now - posted).total_seconds() / 3600, 1),
        "checks": (old.get("checks") or 0) + 1,
        "metrics": merged,
        "below_five": sorted(set(below) - set(merged)),
        "navigation": navigation if navigation is not None else old.get("navigation"),
    }


def main():
    token = env("META_SYSTEM_USER_TOKEN")
    app_secret = env("META_APP_SECRET")
    ig_id = env("META_IG_USER_ID")
    now = datetime.now(timezone.utc)

    check_token(token, app_secret)
    stories = load()
    live = list_live_stories(ig_id, token, app_secret)
    print(f"{len(live)} live Stor{'y' if len(live) == 1 else 'ies'}.")
    measured = failed = 0
    for item in live:
        try:
            metrics, below = fetch_metrics(item["id"], token, app_secret)
            navigation = fetch_navigation(item["id"], token, app_secret)
            upsert(stories, item, now, metrics, below, navigation)
            measured += 1
        except (AuthError, BlockedError):
            raise
        except Exception as exc:  # noqa: BLE001 - one Story must not stop the rest
            failed += 1
            print(f"  WARN Story {item['id']}: {exc}", file=sys.stderr)
            if is_rate_limited(exc):
                print("  Rate-limited: the rest wait for the next hourly run.", file=sys.stderr)
                break
    save(stories)
    print(f"Measured {measured}, failed {failed}. {len(stories)} Stories on record.")


if __name__ == "__main__":
    try:
        main()
    except (AuthError, BlockedError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
