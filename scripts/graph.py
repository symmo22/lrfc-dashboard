"""
Shared Graph API helpers for the LRFC dashboard scripts.

Centralises the retry/backoff logic, auth error handling, and token
derivation used by both ingest.py (account-level) and ingest_posts.py
(post-level), so the two scripts can't drift out of sync on how they
handle a rate limit or an expired token.
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

GRAPH_VERSION = "v24.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

RETRYABLE_META_CODES = {4, 17, 32, 613}
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = [30, 120, 480]


class AuthError(Exception):
    pass


class BlockedError(Exception):
    pass


def env(name):
    value = os.environ.get(name)
    if not value:
        raise BlockedError(f"Missing required environment variable: {name}")
    return value


def appsecret_proof(token, app_secret):
    return hmac.new(
        app_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _do_request(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _handle_http_error(exc, attempt):
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
        if attempt < MAX_ATTEMPTS:
            print(
                f"  Retryable error ({code}): {message}. "
                f"Waiting {BACKOFF_SECONDS[attempt - 1]}s before retry "
                f"{attempt + 1}/{MAX_ATTEMPTS}...",
                file=sys.stderr,
            )
            time.sleep(BACKOFF_SECONDS[attempt - 1])
            return True
        raise RuntimeError(f"Gave up after {MAX_ATTEMPTS} attempts: {message}")

    raise RuntimeError(f"Non-retryable error ({code}): {message}")


def graph_get(path, token, app_secret, params=None):
    params = dict(params or {})
    params["access_token"] = token
    params["appsecret_proof"] = appsecret_proof(token, app_secret)
    url = f"{GRAPH_BASE}/{path}?{urllib.parse.urlencode(params)}"
    return graph_get_url(url)


def graph_get_url(url):
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _do_request(url)
        except urllib.error.HTTPError as exc:
            if _handle_http_error(exc, attempt):
                continue
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = str(exc)
            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF_SECONDS[attempt - 1])
                continue
            raise RuntimeError(f"Network error after {MAX_ATTEMPTS} attempts: {last_error}")

    raise RuntimeError(f"Unreachable: {last_error}")


def check_token(token, app_secret):
    graph_get("me", token, app_secret, {"fields": "id"})


def get_page_token(page_id, system_user_token, app_secret):
    result = graph_get(page_id, system_user_token, app_secret, {"fields": "access_token"})
    return result["access_token"]


def ping_heartbeat():
    url = os.environ.get("HEARTBEAT_URL")
    if not url:
        return
    try:
        urllib.request.urlopen(url, timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"Heartbeat ping failed (non-fatal): {exc}", file=sys.stderr)


def is_rate_limited(exc):
    """graph_get raises 'Gave up after N attempts' only after exhausting its
    retries on a rate-limit or transient server error."""
    return str(exc).startswith("Gave up after")


class Notes:
    """Runs optional fetches, turning failures into notes instead of crashes.

    Circuit breaker: once a platform is rate-limited (retries exhausted),
    every later call for that platform in the same run is skipped rather
    than each one waiting minutes through its own retries. Without this, a
    rate-limited backfill could hang the job for hours and lose the whole
    day's commit. The platform is taken from the first word of the label
    ("Instagram ..." / "Facebook ..."); skipped work is retried next run."""

    def __init__(self):
        self.items = []
        self.stopped = set()
        self.skipped = 0

    def attempt(self, label, fn):
        platform = label.split()[0]
        if platform in self.stopped:
            self.skipped += 1
            return None
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - optional figure, never fatal
            self.items.append(f"{label}: {exc}")
            print(f"  WARN {label}: {exc}", file=sys.stderr)
            if is_rate_limited(exc):
                self.stopped.add(platform)
                print(f"  {platform} rate-limited: skipping its remaining calls this run.", file=sys.stderr)
            return None

    def summary(self):
        text = f"{len(self.items)} note(s)"
        if self.stopped:
            text += f"; rate-limited: {', '.join(sorted(self.stopped))}, {self.skipped} call(s) left for the next run"
        return text
