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
  - Instagram collab posts. The /media edge only returns posts this account
    created. Collab posts another account created (for example the Women's
    account) with this account as an accepted collaborator come from the
    separate collaborative_media edge (added by Meta in April 2026). Both
    kinds are flagged in a "collab" field. A collab post is one post with
    one pooled set of figures, shared by every account it appears on.
    Posts the Women's account publishes without inviting this account are
    not visible to this token at all.

Required environment variables: same four as ingest.py.
"""

import json
import re
import sys
import zlib
from datetime import date, datetime, timezone
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
    is_rate_limited,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
POSTS_FILE = DATA_DIR / "posts.json"
COLLAB_STATUS_FILE = DATA_DIR / "collab_status.json"
# Instagram keeps per-post insights for 2 years; a couple of days' margin.
LOOKBACK_DAYS = 728
# Posts younger than this are refreshed every run. Older posts' lifetime
# figures barely move, so each is refreshed once a week, spread across the
# week by post ID so no single run does them all.
DAILY_REFRESH_DAYS = 90
# Cap on never-measured posts per run, newest first. Keeps the first run
# after widening the lookback (~800 posts) inside Meta's rate limits; the
# backlog clears over a few runs.
MAX_NEW_PER_RUN = 100
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
        "id,message,created_time,permalink_url,full_picture,"
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
        # Meta's image URLs are signed and expire after a few days. Every
        # post inside LOOKBACK_DAYS is re-listed each run, so the stored URL
        # is refreshed daily; the dashboard hides any that have lapsed.
        "image": item.get("full_picture"),
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


IG_FIELDS = (
    "id,caption,media_type,media_product_type,timestamp,permalink,"
    "like_count,comments_count,media_url,thumbnail_url"
)
# The collaborators edge, requested inline so it costs no extra calls.
COLLABORATORS_FIELD = "collaborators{username,invite_status}"
# Documented on IG Media as readable by accepted collaborators; requested on
# the collab listing, with a fallback listing without them if refused.
COLLAB_COUNT_FIELDS = "shares_count,saved_count"


def list_instagram_media(ig_id, token, app_secret, cutoff_date, status):
    """Posts this account created. Asks for each post's collaborators too;
    if Meta refuses that, lists without it and records why, so a refusal
    can never cost the core post figures."""
    try:
        result = graph_get(f"{ig_id}/media", token, app_secret,
                           {"fields": f"{IG_FIELDS},{COLLABORATORS_FIELD}", "limit": 25})
        status["collaborators_field"] = "ok"
    except AuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - optional enrichment only
        status["collaborators_field"] = f"unavailable: {exc}"
        print(f"  WARN collaborators not available, listing without them: {exc}", file=sys.stderr)
        result = graph_get(f"{ig_id}/media", token, app_secret, {"fields": IG_FIELDS, "limit": 25})
    return page_through(result, cutoff_date)


def list_collaborative_media(ig_id, token, app_secret, cutoff_date, status):
    """Posts other accounts created with this account as an accepted
    collaborator. Tried on the pinned API version, then the next one, in
    case Meta gated the new edge by version. Never fatal: a failure is
    recorded and the run carries on without collab posts."""
    errors = []
    attempts = [(v, f) for v in (None, "v25.0")
                for f in (f"{IG_FIELDS},username,{COLLAB_COUNT_FIELDS}", f"{IG_FIELDS},username")]
    for version, fields in attempts:
        try:
            result = graph_get(f"{ig_id}/collaborative_media", token, app_secret,
                               {"fields": fields, "limit": 25}, version=version)
            # Meta doesn't document this edge's order, so filter by date
            # rather than stopping at the first old post.
            items = page_through(result, cutoff_date, newest_first=False)
            status["collaborative_media"] = "ok"
            status["collaborative_media_version"] = version or "default"
            status["collab_count_fields"] = COLLAB_COUNT_FIELDS in fields
            return items
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - includes BlockedError: optional, never fatal
            errors.append(f"{version or 'default'}: {exc}")
    status["collaborative_media"] = "unavailable: " + " | ".join(errors)
    print(f"  WARN collab posts not available: {status['collaborative_media']}", file=sys.stderr)
    return []


def page_through(result, cutoff_date, newest_first=True):
    items = []
    while True:
        for item in result.get("data", []):
            posted = datetime.fromisoformat(
                item["timestamp"].replace("Z", "+00:00")
            ).date()
            if posted < cutoff_date:
                if newest_first:
                    return items
                continue
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


def collab_listing_figures(item):
    """Figures for a collab post another account created, read from the
    collaborative_media listing itself. No per-post call: on 2026-09-27
    Meta refused both insights and a plain field lookup on every one of
    these posts ("Object ... cannot be loaded due to missing permissions"),
    which is how Meta treats a post owned by an account this token has no
    access to. So reach is not available for them and is never guessed;
    the dashboard keeps them out of reach rankings and averages. Likes and
    comments come through static_metrics from the same listing."""
    metrics = {"counts_only": True}
    if item.get("shares_count") is not None:
        metrics["shares"] = item["shares_count"]
    if item.get("saved_count") is not None:
        metrics["saved"] = item["saved_count"]
    return metrics


def collab_info(item):
    """None for an ordinary post. For a collab post: who created it and who
    else it appears with. role 'host' = this account created it and invited
    others; 'guest' = another account created it and invited this one."""
    if item.get("username"):  # only requested on the collaborative_media edge
        return {"role": "guest", "owner": item["username"]}
    accepted = [c.get("username") for c in (item.get("collaborators") or {}).get("data", [])
                if c.get("username") and str(c.get("invite_status", "Accepted")).lower() == "accepted"]
    if accepted:
        return {"role": "host", "with": sorted(accepted)}
    return None


def build_instagram_record(item):
    caption = item.get("caption", "") or ""
    return {
        "collab": collab_info(item),
        "platform": "instagram",
        "post_id": item["id"],
        "permalink": item.get("permalink"),
        "published_at": item["timestamp"],
        "format": instagram_format(item),
        "caption": caption,
        # thumbnail_url first: for videos and Reels media_url is the video
        # file itself, not a picture.
        "image": item.get("thumbnail_url") or item.get("media_url"),
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


def refresh_status(record, existing, today):
    """'new' if never measured, 'due' if it should be re-measured this run,
    None if its current figures are fresh enough."""
    latest = (existing.get("latest") or {}).get("date")
    if not latest and not existing.get("snapshots"):
        return "new"
    published = datetime.fromisoformat(record["published_at"].replace("Z", "+00:00")).date()
    if (today - published).days <= DAILY_REFRESH_DAYS:
        return "due"
    if not latest or (today - date.fromisoformat(latest)).days >= 8:
        return "due"
    if zlib.crc32(record["post_id"].encode()) % 7 == today.weekday():
        return "due"
    return None


def post_key(record):
    return f"{record['platform']}:{record['post_id']}"


def upsert_post(posts, record, today_str, metrics, keep_snapshot):
    """Every post in the window gets its latest lifetime numbers on every
    run ("latest"). Dated checkpoint snapshots are kept only on decay-
    schedule days, or when a post has never been snapshotted, so the file
    doesn't grow by one entry per post per day.

    An earlier version only fetched numbers on schedule days, which meant
    posts first seen on an off-schedule day were listed but never
    measured: 172 of the first 207 posts, found on 2026-09-21. Fixed here.

    metrics=None (a failed fetch) keeps whatever was there before."""
    key = post_key(record)
    existing = posts.get(key, {})
    snapshots = existing.get("snapshots", {})
    latest = existing.get("latest")
    if metrics is not None:
        latest = {"date": today_str, "metrics": metrics}
        if keep_snapshot:
            snapshots[today_str] = metrics
    posts[key] = {**record, "snapshots": snapshots, "latest": latest}


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

    def refresh(label, items, build, fetch):
        measured, failed, new, waiting = 0, 0, 0, 0
        limited = False  # circuit breaker: see graph.Notes
        for item in items:
            record = build(item)
            existing = posts.get(post_key(record), {})
            status = refresh_status(record, existing, today)
            if status and limited:
                waiting += 1
                status = None
            if status == "new" and new >= MAX_NEW_PER_RUN:
                waiting += 1
                status = None
            metrics = None
            if status:
                try:
                    metrics = fetch(item["id"])
                    measured += 1
                    new += status == "new"
                except (AuthError, BlockedError):
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad post must not stop the rest
                    print(f"  WARN {label} post {item['id']}: {exc}", file=sys.stderr)
                    failed += 1
                    if is_rate_limited(exc):
                        limited = True
                        print(f"  {label} rate-limited: remaining posts wait for the next run.", file=sys.stderr)
            keep = snapshot_due(record["published_at"], today) or not existing.get("snapshots")
            upsert_post(posts, record, today_str, metrics, keep)
        print(f"  {label}: measured {measured} ({new} for the first time), {failed} failed, "
              f"{waiting} waiting for a later run.")

    print(f"Listing Facebook posts since {cutoff}...")
    fb_items = list_facebook_posts(page_id, page_token, app_secret, cutoff)
    print(f"  Found {len(fb_items)} post(s) in window.")
    refresh("Facebook", fb_items, build_facebook_record,
            lambda pid: fetch_facebook_post_insights(pid, page_token, app_secret))

    collab_status = {"checked_at": today_str}
    print(f"Listing Instagram media since {cutoff}...")
    ig_items = list_instagram_media(ig_id, token, app_secret, cutoff, collab_status)
    print(f"  Found {len(ig_items)} post(s) in window, "
          f"{sum(1 for i in ig_items if collab_info(i))} of them collabs we created.")
    refresh("Instagram", ig_items, build_instagram_record,
            lambda pid: fetch_instagram_media_insights(pid, token, app_secret))

    # Core figures are saved before the optional collab step, so nothing in
    # it can ever cost the day's main post figures.
    save_posts(posts)

    print("Listing collab posts other accounts created with us...")
    guest_items = list_collaborative_media(ig_id, token, app_secret, cutoff, collab_status)
    own_ids = {i["id"] for i in ig_items}
    guest_items = [i for i in guest_items if i["id"] not in own_ids]  # never count a post twice
    collab_status["collab_posts_found"] = len(guest_items)
    print(f"  Found {len(guest_items)} collab post(s) created by other accounts.")
    guest_by_id = {i["id"]: i for i in guest_items}
    refresh("Instagram collab", guest_items, build_instagram_record,
            lambda pid: collab_listing_figures(guest_by_id[pid]))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COLLAB_STATUS_FILE.write_text(json.dumps(collab_status, indent=2, sort_keys=True), encoding="utf-8")

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
