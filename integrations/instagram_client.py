from __future__ import annotations

from datetime import datetime
from itertools import islice
from typing import Optional
from urllib.parse import urlencode

from integrations.facebook_ads_client import FacebookApiError, _batch_get, _get

MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,permalink,timestamp,"
    "like_count,comments_count,thumbnail_url,media_url"
)
# Meta rejects the whole insights request when one metric does not apply to the
# media type, so each format asks only for metrics it supports.
FEED_METRICS = "reach,views,saved,shares,total_interactions,follows,profile_visits"
REELS_METRICS = "reach,views,saved,shares,total_interactions"
FALLBACK_METRICS = "reach,saved,total_interactions"
INVALID_METRIC_ERROR_CODES = {100}
MEDIA_PAGE_SIZE = 50
INSIGHTS_BATCH_SIZE = 50


def get_instagram_business_accounts() -> list[dict]:
    """Instagram professional accounts linked to Pages the token can access."""
    accounts: list[dict] = []
    payload = _get(
        "me/accounts",
        {"fields": "id,name,instagram_business_account{id,username,followers_count,media_count}", "limit": 100},
    )
    while True:
        for page in payload.get("data", []):
            instagram = page.get("instagram_business_account") or {}
            if instagram.get("id"):
                accounts.append({**instagram, "page_id": page.get("id"), "page_name": page.get("name")})
        next_url = payload.get("paging", {}).get("next")
        if not next_url:
            return accounts
        payload = _get(next_url)


def _parse_timestamp(value: object) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
    except ValueError:
        return None


def get_media(ig_user_id: str, since: datetime, until: datetime) -> list[dict]:
    """Media published in [since, until]; the edge is ordered newest first."""
    media: list[dict] = []
    payload = _get(f"{ig_user_id}/media", {"fields": MEDIA_FIELDS, "limit": MEDIA_PAGE_SIZE})
    while True:
        reached_older = False
        for item in payload.get("data", []):
            published_at = _parse_timestamp(item.get("timestamp"))
            if published_at is None or published_at > until:
                continue
            if published_at < since:
                reached_older = True
                break
            media.append(item)
        next_url = payload.get("paging", {}).get("next")
        if reached_older or not next_url:
            return media
        payload = _get(next_url)


def metrics_for_media(item: dict) -> str:
    return REELS_METRICS if item.get("media_product_type") == "REELS" else FEED_METRICS


def parse_insights(payload: dict) -> dict[str, int]:
    values: dict[str, int] = {}
    for metric in payload.get("data", []):
        name = metric.get("name")
        if not name:
            continue
        total_value = metric.get("total_value") or {}
        if "value" in total_value:
            value = total_value["value"]
        else:
            points = metric.get("values") or [{}]
            value = points[-1].get("value")
        if isinstance(value, (int, float)):
            values[name] = int(value)
    return values


def _insights_path(media_id: str, metrics: str) -> str:
    return f"{media_id}/insights?{urlencode({'metric': metrics})}"


def get_media_insights(media: list[dict]) -> dict[str, dict]:
    """Return ``{media_id: {"metrics": {...}, "error": str | None}}``.

    A post whose insights are unavailable keeps its basic fields; the error is
    stored so the UI can explain the gap instead of failing the whole sync.
    """
    results: dict[str, dict] = {}
    iterator = iter(media)
    while chunk := list(islice(iterator, INSIGHTS_BATCH_SIZE)):
        payload = _batch_get(
            [_insights_path(item["id"], metrics_for_media(item)) for item in chunk],
            allow_item_errors=True,
        )
        for item, body in zip(chunk, payload):
            error = body.get("error") if isinstance(body, dict) else None
            if not error:
                results[item["id"]] = {"metrics": parse_insights(body), "error": None}
                continue
            if error.get("code") in INVALID_METRIC_ERROR_CODES:
                try:
                    fallback = _get(f"{item['id']}/insights", {"metric": FALLBACK_METRICS})
                    results[item["id"]] = {"metrics": parse_insights(fallback), "error": None}
                    continue
                except FacebookApiError as exc:
                    results[item["id"]] = {"metrics": {}, "error": str(exc)}
                    continue
            results[item["id"]] = {
                "metrics": {},
                "error": f"({error.get('code')}) {error.get('message', 'Insights unavailable')}",
            }
    return results
