from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from itertools import islice
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from core.i18n import tr

GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v21.0")
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Graph API errors that are safe to retry after waiting. Code 2 is a temporary
# service failure; the others are throttling/rate-limit responses.
RETRYABLE_ERROR_CODES = {2, 4, 17, 32, 613}
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 2.0
DATA_REDUCTION_DELAY_SECONDS = 1.0


class FacebookApiError(Exception):
    """Raised with the actual Graph API error message so the UI can show it."""

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


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
        try:
            response = requests.get(url, params=query, headers=headers, timeout=30)
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise FacebookApiError(
                tr(
                    f"Facebook API network error after {MAX_RETRIES} attempts: {exc}",
                    f"Сетевая ошибка Facebook API после {MAX_RETRIES} попыток: {exc}",
                )
            ) from exc
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
        if code in RETRYABLE_ERROR_CODES and attempt < MAX_RETRIES - 1:
            time.sleep(backoff)
            backoff *= 2
            continue
        message = error.get("message", "Unknown Facebook API error")
        raise FacebookApiError(f"Facebook API error ({code}): {message}", code=code)

    message = (last_error or {}).get("message", "Rate limited")
    raise FacebookApiError(
        f"Facebook API rate limit exceeded after {MAX_RETRIES} retries: {message}",
        code=(last_error or {}).get("code"),
    )


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


def _batch_get(paths: list[str]) -> list[dict]:
    """Run supported Graph batch GETs without the removed root ``ids`` parameter."""
    token = _access_token()
    batch_data = {"batch": json.dumps([{"method": "GET", "relative_url": path} for path in paths])}
    headers = {"Authorization": f"Bearer {token}"}
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                f"{GRAPH_API_BASE}/",
                data=batch_data,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise FacebookApiError(
                tr(
                    f"Facebook API network error after {MAX_RETRIES} attempts: {exc}",
                    f"Сетевая ошибка Facebook API после {MAX_RETRIES} попыток: {exc}",
                )
            ) from exc
        try:
            payload = response.json()
        except ValueError:
            response.raise_for_status()
            raise FacebookApiError(
                tr(
                    f"Facebook API returned an unreadable batch response (status {response.status_code}).",
                    f"Facebook API вернул нечитаемый пакетный ответ (status {response.status_code}).",
                )
            )

        batch_error: Optional[FacebookApiError] = None
        results: list[dict] = []
        if not response.ok or isinstance(payload, dict):
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = error.get("code")
            message = error.get("message", "Unknown Facebook API batch error")
            batch_error = FacebookApiError(f"Facebook API error ({code}): {message}", code=code)
        else:
            for item in payload:
                try:
                    body = json.loads(item.get("body", "{}"))
                except (TypeError, ValueError):
                    raise FacebookApiError("Facebook API returned an unreadable item in a batch response.")
                if item.get("code", 500) >= 400 or "error" in body:
                    error = body.get("error", {})
                    code = error.get("code")
                    message = error.get("message", "Unknown Facebook API batch item error")
                    batch_error = FacebookApiError(f"Facebook API error ({code}): {message}", code=code)
                    break
                results.append(body)

        if batch_error is None:
            return results
        if batch_error.code in RETRYABLE_ERROR_CODES and attempt < MAX_RETRIES - 1:
            time.sleep(backoff)
            backoff *= 2
            continue
        raise batch_error

    raise FacebookApiError("Facebook API batch retry limit exceeded.")


def get_ads_by_ids(ad_ids: list[str], chunk_size: int = 50) -> list[dict]:
    """Fetch details only for ads that produced insights in the selected period."""
    results: list[dict] = []

    def fetch_chunk(chunk: list[str]) -> None:
        try:
            fields = (
                "id,name,status,adset_id,"
                "campaign{id,name,objective,status,created_time},"
                "creative{id,title,body,image_url,thumbnail_url,video_id,instagram_user_id}"
            )
            payload = _batch_get(
                [f"{ad_id}?{urlencode({'fields': fields})}" for ad_id in chunk]
            )
        except FacebookApiError as exc:
            if exc.code != 1 or len(chunk) == 1:
                raise
            time.sleep(DATA_REDUCTION_DELAY_SECONDS)
            midpoint = len(chunk) // 2
            fetch_chunk(chunk[:midpoint])
            fetch_chunk(chunk[midpoint:])
            return
        results.extend(value for value in payload if isinstance(value, dict) and value.get("id"))

    iterator = iter(dict.fromkeys(ad_ids))
    while chunk := list(islice(iterator, chunk_size)):
        fetch_chunk(chunk)
    return results


def get_instagram_accounts(account_id: str) -> list[dict]:
    payload = _get(account_id, {"fields": "instagram_accounts{id,username}"})
    return payload.get("instagram_accounts", {}).get("data", [])


def _get_insights_window(
    account_id: str, since: date, until: date, region_code: str | None = None
) -> list[dict]:
    params = {
        "level": "ad",
        "fields": (
            "ad_id,date_start,date_stop,spend,impressions,reach,clicks,"
            "actions,cost_per_action_type"
        ),
        "time_range": f'{{"since":"{since.isoformat()}","until":"{until.isoformat()}"}}',
        "time_increment": 1,
        "use_unified_attribution_setting": "true",
    }
    if region_code:
        params["filtering"] = json.dumps(
            [{"field": "campaign.name", "operator": "CONTAIN", "value": f"[r:{region_code}]"}],
            separators=(",", ":"),
        )
    return _get_all_pages(
        f"{account_id}/insights",
        params,
    )


def _get_insights_resilient(
    account_id: str, since: date, until: date, region_code: str | None = None
) -> list[dict]:
    try:
        return _get_insights_window(account_id, since, until, region_code)
    except FacebookApiError as exc:
        if exc.code != 1 or since == until:
            raise
        time.sleep(DATA_REDUCTION_DELAY_SECONDS)
        midpoint = since + timedelta(days=(until - since).days // 2)
        return [
            *_get_insights_resilient(account_id, since, midpoint, region_code),
            *_get_insights_resilient(account_id, midpoint + timedelta(days=1), until, region_code),
        ]


def get_insights(
    account_id: str,
    since: str,
    until: str,
    window_days: int = 7,
    region_code: str | None = None,
) -> list[dict]:
    """Fetch daily insights in small windows and split overloaded requests further."""
    start = date.fromisoformat(since)
    end = date.fromisoformat(until)
    results: list[dict] = []
    window_start = start
    while window_start <= end:
        window_end = min(window_start + timedelta(days=window_days - 1), end)
        results.extend(_get_insights_resilient(account_id, window_start, window_end, region_code))
        window_start = window_end + timedelta(days=1)
    return results
