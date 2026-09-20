#!/usr/bin/env python3
"""
Leamington RFC social dashboard — daily account-level ingestion.

Pulls account-level snapshots (follower count, daily reach, daily views) for
the club's Facebook Page and Instagram account, and upserts them into
data/account_snapshots.json keyed by (platform, date), so re-running the
same day never creates a duplicate row.

Required environment variables (set as GitHub Actions secrets):
  META_SYSTEM_USER_TOKEN
  META_APP_SECRET
  META_PAGE_ID
  META_IG_USER_ID

Optional:
  HEARTBEAT_URL   - pinged on success. Leave unset and this step is skipped.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from graph import (  # noqa: E402
    AuthError,
    BlockedError,
    check_token,
    env,
    get_page_token,
    graph_get,
    ping_heartbeat,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_FILE = DATA_DIR / "account_snapshots.json"


def fetch_facebook_snapshot(page_id, page_token, app_secret, day):
    insights = graph_get(
        f"{page_id}/insights",
        page_token,
        app_secret,
        {"metric": "page_total_media_view_unique,page_media_view", "period": "day"},
    )
    metrics = {}
    for row in insights.get("data", []):
        values = row.get("values", [])
        if values:
            metrics[row["name"]] = values[-1].get("value")

    page_info = graph_get(page_id, page_token, app_secret, {"fields": "fan_count"})
    metrics["fan_count"] = page_info.get("fan_count")

    return {
        "platform": "facebook",
        "date": day,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
    }


def fetch_instagram_snapshot(ig_id, token, app_secret, day):
    insights = graph_get(
        f"{ig_id}/insights",
        token,
        app_secret,
        {"metric": "reach,views", "period": "day", "metric_type": "total_value"},
    )
    metrics = {}
    for row in insights.get("data", []):
        total = row.get("total_value", {})
        metrics[row["name"]] = total.get("value")

    account_info = graph_get(ig_id, token, app_secret, {"fields": "followers_count"})
    metrics["followers_count"] = account_info.get("followers_count")

    return {
        "platform": "instagram",
        "date": day,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
    }


def load_snapshots():
    if SNAPSHOT_FILE.exists():
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    return {}


def save_snapshots(snapshots):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(
        json.dumps(snapshots, indent=2, sort_keys=True), encoding="utf-8"
    )


def upsert(snapshots, record):
    """Overwrite by natural key (platform, date). Never append. This is what
    makes running the job twice in one day, or re-running a backfill, safe."""
    key = f"{record['platform']}:{record['date']}"
    snapshots[key] = record


def main():
    token = env("META_SYSTEM_USER_TOKEN")
    app_secret = env("META_APP_SECRET")
    page_id = env("META_PAGE_ID")
    ig_id = env("META_IG_USER_ID")

    today = datetime.now(timezone.utc).date().isoformat()

    print("Checking token validity...")
    check_token(token, app_secret)

    print("Deriving Page access token...")
    page_token = get_page_token(page_id, token, app_secret)

    print("Fetching Facebook Page snapshot...")
    fb_snapshot = fetch_facebook_snapshot(page_id, page_token, app_secret, today)
    print(f"  {fb_snapshot['metrics']}")

    print("Fetching Instagram snapshot...")
    ig_snapshot = fetch_instagram_snapshot(ig_id, token, app_secret, today)
    print(f"  {ig_snapshot['metrics']}")

    print("Upserting into data/account_snapshots.json...")
    snapshots = load_snapshots()
    upsert(snapshots, fb_snapshot)
    upsert(snapshots, ig_snapshot)
    save_snapshots(snapshots)

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
