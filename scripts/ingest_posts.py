#!/usr/bin/env python3
"""
Leamington RFC social dashboard — post-level ingestion.

Fetches individual posts (last LOOKBACK_DAYS days) for both platforms,
derives the content cuts the methodology doc calls for (format, caption
length bucket, hashtag count, day/time posted), and snapshots each post's
metrics on a decay schedule: day 0, 1, 3, 7, 14, 28, then roughly monthly.

Scope of this version, stated plainly rather than left implicit:
  - Only posts from the last LOOKBACK_DAYS days are even considered each
    run, so a post older than that stops getting new snapshots. Widening
    this is a config change (see LOOKBACK_DAYS below), not a redesign.
  - Instagram Stories are out of scope, same as the methodology doc's
    "what cannot be measured" section - they don't appear on the /media
    edge this script reads from.
  - Facebook video posts and Reels are both recorded as format "video".
    Distinguishing them is a reasonable next refinement, not done here.

Required environment variables: same four as ingest.py.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from graph import (  # noqa: E402
    AuthError,
    BlockedError,
    check_token,
    env,
    get_page_token,
    graph_get,
    graph_get_url,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
POSTS_FILE = DATA_DIR / "posts.json"
LOOKBACK_DAYS = 180
LONDON = ZoneInfo("Europe/London")

HASHTAG_RE = re.compile(r"#\w+")


# ---------------------------------------------------------------------------
# Content cuts — derived from the post itself, no manual tagging needed.
# ---------------------------------------------------------------------------

def caption_length_bucket(length):
    if length < 100:
        return "under_100"
    if length < 300:
        return "100_to_300"
    if length < 600:
        return "300_to_600"
    return "over_600"


def hashtag_bucket(count):
    if count == 0:
        return "0"
    if count <= 3:
        return "1_to_3"
    if count <= 8:
        return "4_to_8"
    return "9_plus"


def local_day_and_hour(iso_timestamp):
    dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    local = dt.astimezone(LONDON)
    return local.strftime("%A"), local.hour


def derive_cuts(caption, published_at_iso):
    caption = caption or ""
    hashtags = HASHTAG_RE.findall(caption)
    day_of_week, hour_local = local_day_and_hour(published_at_iso)
    return {
        "caption_length": len(caption),
        "caption_length_bucket": caption_length_bucket(len(caption)),
        "hashtag_count": len(hashtags),
        "hashtag_bucket": hashtag_bucket(len(hashtags)),
        "day_of_week": day_of_week,
        "hour_local": hour_local,
    }


def snapshot_due(published_at_iso, today):
    published_date = datetime.fromisoformat(
        published_at_iso.replace("Z", "+00:00")
    ).date()
    age_days = (today - published_date).days
    if age_days < 0:
        return False
    if age_days in (0, 1, 3, 7, 14, 28):
        return True
    return age_days > 28 and age_days % 30 == 0


# ---------------------------------------------------------------------------
# Facebook
# ---------------------------------------------------------------------------

def facebook_format(item):
    attachments = item.get("attachments", {}).get("data", [])
    if not attachments:
        return "text_or_link"
    first = attachments[0]
    if first.get("subattachments", {}).get("data"):
        return "carousel"
    media_type = first.get("media_type", "").lower()
    if media_type == "video":
        return "video"
    if media_type == "photo":
        return "image"
    return media_type or "unknown"


def list_facebook_posts(page_id, page_token, app_secret, cutoff_date):
    fields = (
        "id,message,created_time,permalink_url,"
        "attachments{media_type,subattachments{media_type}},"
        "reactions.summary(true),"
        "comments.summary(true),"
        "shares"
    )
    result = graph_get(
        f"{page_id}/posts", page_token, app_secret, {"fields": fields, "limit": 25}
    )
    posts = []
    while True:
        for item in result.get("data", []):
            created = datetime.fromisoformat(
                item["created_time"].replace("Z", "+00:00")
            ).date()
            if created < cutoff_date:
                return posts
            posts.append(item)
        next_url = result.get("paging", {}).get("next")
        if not next_url:
            return posts
        result = graph_get_url(next_url)


def fetch_facebook_post_insights(post_id, page_token, app_secret):
    result = graph_get(
        f"{post_id}/insights",
        page_token,
        app_secret,
        {"metric": "post_total_media_view_unique,post_media_view", "period": "lifetime"},
    )
    metrics = {}
    for row in result.get("data", []):
        # Belt-and-braces: even though we asked for lifetime only, only
        # ever trust a row actually labelled lifetime. A prior version of
        # this function didn't check this, and a same-named "day" row with
        # a real 0 (these newer metrics can lack daily buckets even when
        # the lifetime total is populated) silently overwrote a correct
        # lifetime value with that 0. Confirmed against real data on
        # 2026-09-20: a post with 173 lifetime unique viewers was reported
        # as 0 because of exactly this collision.
        if row.get("period") != "lifetime":
            continue
        values = row.get("values", [])
        if values:
            metrics[row["name"]] = values[-1].get("value")
    return metrics


def build_facebook_record(item):
    return {
        "platform": "facebook",
        "post_id": item["id"],
        "permalink": item.get("permalink_url"),
        "published_at": item["created_time"],
        "format": facebook_format(item),
        "caption": item.get("message", ""),
        **derive_cuts(item.get("message", ""), item["created_time"]),
        "static_metrics": {
            "reactions": item.get("reactions", {}).get("summary", {}).get("total_count"),
            "comments": item.get("comments", {}).get("summary", {}).get("total_count"),
            "shares": item.get("shares", {}).get("count", 0),
        },
    }


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

def instagram_format(item):
    media_type = item.get("media_type", "")
    product_type = item.get("media_product_type", "")
    if product_type == "REELS":
        return "reel"
    if media_type == "CAROUSEL_ALBUM":
        return "carousel"
    if media_type == "VIDEO":
        return "video"
    if media_type == "IMAGE":
        return "image"
    return media_type.lower() or "unknown"


def list_instagram_media(ig_id, token, app_secret, cutoff_date):
    fields = "id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count"
    result = graph_get(f"{ig_id}/media", token, app_secret, {"fields": fields, "limit": 25})
    items = []
    while True:
        for item in result.get("data", []):
            posted = datetime.fromisoformat(
                item["timestamp"].replace("Z", "+00:00")
            ).date()
            if posted < cutoff_date:
                return items
            items.append(item)
        next_url = result.get("paging", {}).get("next")
        if not next_url:
            return items
        result = graph_get_url(next_url)


def fetch_instagram_media_insights(media_id, token, app_secret):
    # No explicit period here, unlike the Facebook call above: Instagram
    # media insights haven't shown the same multi-period response in
    # testing, and this endpoint doesn't universally accept a period
    # parameter the way the account-level and Facebook post endpoints do.
    # The defensive check below still applies in case that ever changes.
    result = graph_get(
        f"{media_id}/insights",
        token,
        app_secret,
        {"metric": "reach,views,shares,saved,total_interactions"},
    )
    metrics = {}
    for row in result.get("data", []):
        if row.get("period") not in (None, "lifetime"):
            continue
        values = row.get("values", [])
        if values:
            metrics[row["name"]] = values[-1].get("value")
        elif "total_value" in row:
            metrics[row["name"]] = row["total_value"].get("value")
    return metrics


def build_instagram_record(item):
    caption = item.get("caption", "") or ""
    return {
        "platform": "instagram",
        "post_id": item["id"],
        "permalink": item.get("permalink"),
        "published_at": item["timestamp"],
        "format": instagram_format(item),
        "caption": caption,
        **derive_cuts(caption, item["timestamp"]),
        "static_metrics": {
            "likes": item.get("like_count"),
            "comments": item.get("comments_count"),
        },
    }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def load_posts():
    if POSTS_FILE.exists():
        return json.loads(POSTS_FILE.read_text(encoding="utf-8"))
    return {}


def save_posts(posts):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    POSTS_FILE.write_text(json.dumps(posts, indent=2, sort_keys=True), encoding="utf-8")


def upsert_post(posts, record, snapshot_date, snapshot_metrics):
    """Static fields (format, caption, cuts) overwrite each run in case they
    change; the snapshots dict only ever gains new dated entries, never
    loses old ones - that's the decay curve building up over time. Running
    the same day twice overwrites that one day's entry, never duplicates."""
    key = f"{record['platform']}:{record['post_id']}"
    existing = posts.get(key, {"snapshots": {}})
    snapshots = existing.get("snapshots", {})
    if snapshot_metrics is not None:
        snapshots[snapshot_date] = snapshot_metrics
    posts[key] = {**record, "snapshots": snapshots}


# ---------------------------------------------------------------------------

def main():
    token = env("META_SYSTEM_USER_TOKEN")
    app_secret = env("META_APP_SECRET")
    page_id = env("META_PAGE_ID")
    ig_id = env("META_IG_USER_ID")

    today = datetime.now(timezone.utc).date()
    cutoff = today.fromordinal(today.toordinal() - LOOKBACK_DAYS)
    today_str = today.isoformat()

    print("Checking token validity...")
    check_token(token, app_secret)

    print("Deriving Page access token...")
    page_token = get_page_token(page_id, token, app_secret)

    posts = load_posts()

    print(f"Listing Facebook posts since {cutoff}...")
    fb_items = list_facebook_posts(page_id, page_token, app_secret, cutoff)
    print(f"  Found {len(fb_items)} post(s) in window.")
    fb_snapshotted = 0
    for item in fb_items:
        record = build_facebook_record(item)
        if snapshot_due(record["published_at"], today):
            metrics = fetch_facebook_post_insights(item["id"], page_token, app_secret)
            fb_snapshotted += 1
        else:
            metrics = None
        upsert_post(posts, record, today_str, metrics)
    print(f"  Snapshotted {fb_snapshotted} Facebook post(s) today.")

    print(f"Listing Instagram media since {cutoff}...")
    ig_items = list_instagram_media(ig_id, token, app_secret, cutoff)
    print(f"  Found {len(ig_items)} post(s) in window.")
    ig_snapshotted = 0
    for item in ig_items:
        record = build_instagram_record(item)
        if snapshot_due(record["published_at"], today):
            metrics = fetch_instagram_media_insights(item["id"], token, app_secret)
            ig_snapshotted += 1
        else:
            metrics = None
        upsert_post(posts, record, today_str, metrics)
    print(f"  Snapshotted {ig_snapshotted} Instagram post(s) today.")

    print("Saving data/posts.json...")
    save_posts(posts)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except (AuthError, BlockedError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
