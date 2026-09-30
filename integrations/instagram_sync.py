from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional

from core.config import now_utc
from core.csv_import import is_novakid_account
from core.database import connect_db
from integrations.facebook_ads_client import FacebookApiError
from integrations.facebook_ads_sync import _acquire_sync_lock, _log_finish, _log_start, _release_sync_lock
from integrations.instagram_client import get_instagram_business_accounts, get_media, get_media_insights

MAX_SYNC_DAYS = 365
CAPTION_MAX_CHARS = 500
# Instagram syncs share the Facebook sync lock/log tables under a separate key
# namespace so an ad account and an Instagram profile never block each other.
LOCK_PREFIX = "ig:"


@dataclass
class InstagramSyncResult:
    username: str
    posts: int = 0
    insights_missing: int = 0
    status: str = "ok"
    message: str = ""


def discover_accounts() -> list[dict]:
    """Store Novakid Instagram accounts reachable with the configured token."""
    accounts = [
        account for account in get_instagram_business_accounts()
        if is_novakid_account(account.get("username"))
    ]
    updated_at = now_utc()
    with connect_db() as conn:
        for account in accounts:
            conn.execute(
                """
                INSERT INTO ig_accounts(
                    ig_user_id, username, page_id, page_name, followers_count, media_count, is_active, updated_at
                ) VALUES(?,?,?,?,?,?,1,?)
                ON CONFLICT(ig_user_id) DO UPDATE SET
                    username=excluded.username,
                    page_id=excluded.page_id,
                    page_name=excluded.page_name,
                    followers_count=excluded.followers_count,
                    media_count=excluded.media_count,
                    updated_at=excluded.updated_at
                """,
                (
                    account["id"], str(account["username"]).lower(), account.get("page_id"), account.get("page_name"),
                    account.get("followers_count"), account.get("media_count"), updated_at,
                ),
            )
        conn.commit()
    return accounts


def preview_url(item: dict) -> Optional[str]:
    if item.get("thumbnail_url"):
        return item["thumbnail_url"]
    if item.get("media_type") != "VIDEO":
        return item.get("media_url")
    return None


def media_row(ig_user_id: str, username: str, item: dict, insights: dict, synced_at: str) -> tuple:
    metrics = insights.get("metrics", {})
    published_at = str(item.get("timestamp") or "")[:19] or None
    return (
        item["id"], ig_user_id, username, published_at, published_at[:7] if published_at else None,
        item.get("media_type"), item.get("media_product_type"), item.get("permalink"),
        (item.get("caption") or "")[:CAPTION_MAX_CHARS] or None, preview_url(item),
        item.get("like_count"), item.get("comments_count"),
        metrics.get("reach"), metrics.get("views"), metrics.get("saved"), metrics.get("shares"),
        metrics.get("total_interactions"), metrics.get("follows"), metrics.get("profile_visits"),
        insights.get("error"), synced_at,
    )


def sync_instagram_account(
    ig_user_id: str, username: str, since: date, until: date, triggered_by: str = "system"
) -> InstagramSyncResult:
    result = InstagramSyncResult(username=username)
    if since > until:
        result.status = "error"
        result.message = "The period start must not be later than its end."
        return result
    if (until - since).days + 1 > MAX_SYNC_DAYS:
        result.status = "error"
        result.message = f"Select a period of no more than {MAX_SYNC_DAYS} days."
        return result

    lock_key = f"{LOCK_PREFIX}{ig_user_id}"
    with connect_db() as conn:
        acquired, reason = _acquire_sync_lock(conn, lock_key)
        if not acquired:
            result.status = "skipped"
            result.message = f"@{username}: {reason}"
            return result
        log_id = _log_start(conn, lock_key, triggered_by, since, until)
        try:
            media = get_media(ig_user_id, datetime.combine(since, time.min), datetime.combine(until, time.max))
            insights = get_media_insights(media)
            synced_at = now_utc()
            conn.executemany(
                """
                INSERT INTO ig_media(
                    media_id, ig_user_id, account, published_at, month, media_type, media_product_type,
                    permalink, caption, preview_url, like_count, comments_count, reach, views, saved,
                    shares, total_interactions, follows, profile_visits, insights_error, synced_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(media_id) DO UPDATE SET
                    ig_user_id=excluded.ig_user_id,
                    account=excluded.account,
                    published_at=excluded.published_at,
                    month=excluded.month,
                    media_type=excluded.media_type,
                    media_product_type=excluded.media_product_type,
                    permalink=excluded.permalink,
                    caption=excluded.caption,
                    preview_url=excluded.preview_url,
                    like_count=excluded.like_count,
                    comments_count=excluded.comments_count,
                    reach=excluded.reach,
                    views=excluded.views,
                    saved=excluded.saved,
                    shares=excluded.shares,
                    total_interactions=excluded.total_interactions,
                    follows=excluded.follows,
                    profile_visits=excluded.profile_visits,
                    insights_error=excluded.insights_error,
                    synced_at=excluded.synced_at
                """,
                [media_row(ig_user_id, username, item, insights.get(item["id"], {}), synced_at) for item in media],
            )
            conn.commit()
            result.posts = len(media)
            result.insights_missing = sum(1 for item in media if insights.get(item["id"], {}).get("error"))
            result.message = f"@{username}: {result.posts} posts"
            if result.insights_missing:
                result.message += f", insights unavailable for {result.insights_missing}"
            _log_finish(conn, log_id, "ok", result.message)
        except FacebookApiError as exc:
            conn.rollback()
            result.status = "error"
            result.message = f"@{username}: {exc}"
            _log_finish(conn, log_id, "error", result.message)
        except Exception as exc:
            conn.rollback()
            result.status = "error"
            result.message = f"@{username}: Unexpected error: {exc}"
            _log_finish(conn, log_id, "error", result.message)
        finally:
            _release_sync_lock(lock_key)
    return result
