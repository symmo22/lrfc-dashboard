#!/usr/bin/env python3
"""
Leamington RFC social dashboard — daily ingestion.

Pulls account-level snapshots (follower count, daily reach, daily views) for
the club's Facebook Page and Instagram account, and upserts them into
data/account_snapshots.json keyed by (platform, date), so re-running the
same day never creates a duplicate row.

This is the account-level slice only. Post-level data (one row per
published post) is the next piece to add, once this is confirmed running
against real data on a real schedule.

Required environment variables (set as GitHub Actions secrets):
  META_SYSTEM_USER_TOKEN
  META_APP_SECRET
  META_PAGE_ID
  META_IG_USER_ID

Optional:
  HEARTBEAT_URL   - pinged on success. Leave unset and this step is skipped,
                    nothing else changes. See healthchecks.io or cron-job.org.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GRAPH_VERSION = "v24.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_FILE = DATA_DIR / "account_snapshots.json"

# Errors worth retrying: Meta's own rate-limit codes, plus standard
# transient HTTP failures. Everything else fails fast rather than burning
# the retry budget on something that will never succeed by waiting.
RETRYABLE_META_CODES = {4, 17, 32, 613}
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = [30, 120, 480]


class AuthError(Exception):
    """Token is invalid or expired. Stop the whole run immediately."""


class BlockedError(Exception):
    """Something only a human can fix. Stop the whole run and say so plainly."""


def env(name):
    value = os.environ.get(name)
    if not value:
        raise BlockedError(f"Missing required environment variable: {name}")
    return value


def appsecret_proof(token, app_secret):
    return hmac.new(
        app_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def graph_get(path, token, app_secret, params=None):
    """One GET call against the Graph API, with retry/backoff on transient errors."""
    params = dict(params or {})
    params["access_token"] = token
    params["appsecret_proof"] = appsecret_proof(token, app_secret)
    url = f"{GRAPH_BASE}/{path}?{urllib.parse.urlencode(params)}"

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                error = json.loads(body).get("error", {})
            except json.JSONDecodeError:
                error = {}
            code = error.get("code")
            message = error.get("message", body)

            if code == 190:
                raise AuthError(f"Token invalid or expired: {message}")
            if isinstance(message, str) and "permission" in message.lower():
                raise BlockedError(f"Permission error, needs a human: {message}")

            if exc.code in RETRYABLE_HTTP_STATUS or code in RETRYABLE_META_CODES:
                last_error = message
                if attempt < MAX_ATTEMPTS:
                    print(
                        f"  Retryable error ({code}): {message}. "
                        f"Waiting {BACKOFF_SECONDS[attempt - 1]}s before retry "
                        f"{attempt + 1}/{MAX_ATTEMPTS}...",
                        file=sys.stderr,
                    )
                    time.sleep(BACKOFF_SECONDS[attempt - 1])
                    continue
                raise RuntimeError(
                    f"Gave up after {MAX_ATTEMPTS} attempts: {message}"
                )

            # Anything else: don't retry, this call has failed for good.
            raise RuntimeError(f"Non-retryable error ({code}): {message}")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = str(exc)
            if attempt < MAX_ATTEMPTS:
                print(
                    f"  Network error: {last_error}. "
                    f"Waiting {BACKOFF_SECONDS[attempt - 1]}s before retry...",
                    file=sys.stderr,
                )
                time.sleep(BACKOFF_SECONDS[attempt - 1])
                continue
            raise RuntimeError(f"Network error after {MAX_ATTEMPTS} attempts: {last_error}")

    raise RuntimeError(f"Unreachable: {last_error}")


def check_token(token, app_secret):
    graph_get("me", token, app_secret, {"fields": "id"})


def get_page_token(page_id, system_user_token, app_secret):
    """Facebook Page insights need a Page-specific token, not the raw system
    user token. Instagram insights don't have this requirement."""
    result = graph_get(page_id, system_user_token, app_secret, {"fields": "access_token"})
    return result["access_token"]


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


def ping_heartbeat():
    url = os.environ.get("HEARTBEAT_URL")
    if not url:
        return
    try:
        urllib.request.urlopen(url, timeout=10)
    except Exception as exc:  # noqa: BLE001 - a failed ping must never fail the run
        print(f"Heartbeat ping failed (non-fatal): {exc}", file=sys.stderr)


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
    ping_heartbeat()


if __name__ == "__main__":
    try:
        main()
    except (AuthError, BlockedError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
