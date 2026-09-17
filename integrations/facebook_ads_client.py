from __future__ import annotations

import os
import time
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from core.i18n import tr

GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v21.0")
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Graph API error codes that mean "you're being rate limited, back off and retry".
RATE_LIMIT_ERROR_CODES = {4, 17, 32, 613}
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 2.0


class FacebookApiError(Exception):
    """Raised with the actual Graph API error message so the UI can show it."""


class FacebookApiNotConfigured(FacebookApiError):
    """Raised when META_ACCESS_TOKEN is missing."""


def _access_token() -> str:
    token = os.getenv("META_ACCESS_TOKEN", "").strip()
    if not token:
        raise FacebookApiNotConfigured(
            tr(
                "META_ACCESS_TOKEN is not set. Add a system token to environment variables to enable Facebook Ads sync.",
                "META_ACCESS_TOKEN не задан. Добавьте системный токен в переменные окружения, "
                "чтобы включить синхронизацию с Facebook Ads.",
            )
        )
    return token


def is_configured() -> bool:
    return bool(os.getenv("META_ACCESS_TOKEN", "").strip())


def _get(path: str, params: Optional[dict] = None) -> dict:
    token = _access_token()
    url = path if path.startswith("http") else f"{GRAPH_API_BASE}/{path.lstrip('/')}"
    # Meta pagination links may echo access_token in their query string. Strip
    # it before issuing the next request so credentials never travel in a URL.
    if path.startswith("http"):
        parts = urlsplit(url)
        safe_query = urlencode([(key, value) for key, value in parse_qsl(parts.query) if key != "access_token"])
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, safe_query, parts.fragment))
    query = dict(params or {})
    # Keep credentials out of URLs, which are commonly captured by proxies,
    # access logs, monitoring tools, and exception reports.
    headers = {"Authorization": f"Bearer {token}"}

    backoff = INITIAL_BACKOFF_SECONDS
    last_error: Optional[dict] = None
    for attempt in range(MAX_RETRIES):
        response = requests.get(url, params=query, headers=headers, timeout=30)
        try:
            payload = response.json()
        except ValueError:
            response.raise_for_status()
            raise FacebookApiError(tr(f"Facebook API returned an unreadable response (status {response.status_code}).", f"Facebook API вернул нечитаемый ответ (status {response.status_code})."))

        if response.ok and "error" not in payload:
            return payload

        error = payload.get("error", {})
        last_error = error
        code = error.get("code")
        if code in RATE_LIMIT_ERROR_CODES and attempt < MAX_RETRIES - 1:
            time.sleep(backoff)
            backoff *= 2
            continue
        message = error.get("message", "Unknown Facebook API error")
        raise FacebookApiError(f"Facebook API error ({code}): {message}")

    message = (last_error or {}).get("message", "Rate limited")
    raise FacebookApiError(f"Facebook API rate limit exceeded after {MAX_RETRIES} retries: {message}")


def _get_all_pages(path: str, params: Optional[dict] = None) -> list[dict]:
    results: list[dict] = []
    payload = _get(path, params)
    results.extend(payload.get("data", []))
    next_url = payload.get("paging", {}).get("next")
    while next_url:
        payload = _get(next_url)
        results.extend(payload.get("data", []))
        next_url = payload.get("paging", {}).get("next")
    return results


def get_campaigns(account_id: str) -> list[dict]:
    return _get_all_pages(
        f"{account_id}/campaigns",
        {"fields": "id,name,objective,status,created_time"},
    )


def get_ads(account_id: str) -> list[dict]:
    """Fetch all ads and their creative metadata in one paginated account query."""
    return _get_all_pages(
        f"{account_id}/ads",
        {
            "fields": (
                "id,name,status,adset_id,campaign_id,"
                "creative{id,title,body,image_url,thumbnail_url,video_id}"
            )
        },
    )


def get_insights(account_id: str, since: str, until: str) -> list[dict]:
    return _get_all_pages(
        f"{account_id}/insights",
        {
            "level": "ad",
            "fields": "ad_id,date_start,date_stop,spend,impressions,reach,clicks",
            "time_range": f'{{"since":"{since}","until":"{until}"}}',
            "time_increment": 1,
        },
    )
