from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from core.config import now_utc
from core.database import connect_db
from integrations.facebook_ads_client import FacebookApiError, get_ads_by_ids, get_insights, get_instagram_accounts

MAX_SYNC_DAYS = 90
SYNC_COOLDOWN_MINUTES = 5
SYNC_LOCK_STALE_MINUTES = 30


def _is_instagram_follow_action(action_type: object) -> bool:
    normalized = str(action_type or "").strip().lower()
    if not normalized or "unfollow" in normalized:
        return False
    if normalized == "like" or "facebook" in normalized or "page" in normalized:
        return False
    return "follow" in normalized


def _instagram_followers_from_insight(row: dict) -> float:
    # Meta reports some conversion-style actions (e.g. instagram_profile_follow,
    # added August 2026) only under "conversions", not "actions". Dedupe by
    # action_type so an action present in both fields isn't counted twice.
    by_type: dict[str, float] = {}
    for field_name in ("actions", "conversions"):
        for action in row.get(field_name) or []:
            action_type = str(action.get("action_type") or "")
            if action_type and _is_instagram_follow_action(action_type) and action_type not in by_type:
                by_type[action_type] = float(action.get("value", 0) or 0)
    return sum(by_type.values())


@dataclass
class SyncResult:
    account_id: str
    campaigns: int = 0
    ads: int = 0
    creatives: int = 0
    insight_rows: int = 0
    status: str = "ok"
    message: str = ""


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _acquire_sync_lock(conn, account_id: str) -> tuple[bool, str]:
    """Atomically acquire an account lock and enforce a short sync cooldown."""
    now = datetime.utcnow()
    stale_before = (now - timedelta(minutes=SYNC_LOCK_STALE_MINUTES)).isoformat(timespec="seconds") + "Z"
    conn.execute("DELETE FROM fb_sync_locks WHERE acquired_at < ?", (stale_before,))
    cur = conn.execute(
        """
        INSERT INTO fb_sync_locks(account_id, acquired_at) VALUES(?,?)
        ON CONFLICT(account_id) DO NOTHING
        RETURNING account_id
        """,
        (account_id, now_utc()),
    )
    if cur.fetchone() is None:
        conn.commit()
        return False, "Synchronization for this ad account is already running."

    last = conn.execute(
        """
        SELECT finished_at FROM fb_sync_log
        WHERE account_id=? AND status='ok' AND finished_at IS NOT NULL
        ORDER BY finished_at DESC LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    if last:
        elapsed = now - _parse_utc(last[0])
        if elapsed < timedelta(minutes=SYNC_COOLDOWN_MINUTES):
            remaining = max(1, int((timedelta(minutes=SYNC_COOLDOWN_MINUTES) - elapsed).total_seconds()) + 1)
            conn.execute("DELETE FROM fb_sync_locks WHERE account_id=?", (account_id,))
            conn.commit()
            return False, f"Please wait {remaining} seconds before syncing this ad account again."
    conn.commit()
    return True, ""


def _release_sync_lock(account_id: str) -> None:
    with connect_db() as conn:
        conn.execute("DELETE FROM fb_sync_locks WHERE account_id=?", (account_id,))
        conn.commit()


def _log_start(conn, account_id: str, triggered_by: str, since: date, until: date) -> int:
    cur = conn.execute(
        """INSERT INTO fb_sync_log(
               account_id, started_at, status, triggered_by, period_start, period_end
           ) VALUES(?,?,?,?,?,?) RETURNING id""",
        (account_id, now_utc(), "running", triggered_by, since.isoformat(), until.isoformat()),
    )
    log_id = int(cur.fetchone()[0])
    conn.commit()
    return log_id


def _log_finish(conn, log_id: int, status: str, message: str) -> None:
    conn.execute(
        "UPDATE fb_sync_log SET finished_at=?, status=?, message=? WHERE id=?",
        (now_utc(), status, message, log_id),
    )
    conn.commit()


def sync_ad_account(
    account_id: str,
    since: date,
    until: date,
    triggered_by: str = "system",
    region_code: str | None = None,
    profile_name: str | None = None,
) -> SyncResult:
    result = SyncResult(account_id=account_id)
    if since > until:
        result.status = "error"
        result.message = "The period start must not be later than its end."
        return result
    if (until - since).days + 1 > MAX_SYNC_DAYS:
        result.status = "error"
        result.message = f"Select a period of no more than {MAX_SYNC_DAYS} days."
        return result
    with connect_db() as conn:
        acquired, reason = _acquire_sync_lock(conn, account_id)
        if not acquired:
            result.status = "skipped"
            result.message = reason
            return result
        log_id = _log_start(conn, account_id, triggered_by, since, until)
        try:
            updated_at = now_utc()

            insights = get_insights(
                account_id,
                since.isoformat(),
                until.isoformat(),
                region_code=region_code,
            )
            ads = get_ads_by_ids([row["ad_id"] for row in insights if row.get("ad_id")])
            campaigns_by_id = {
                ad["campaign"]["id"]: ad["campaign"]
                for ad in ads
                if ad.get("campaign", {}).get("id")
            }
            campaigns = list(campaigns_by_id.values())
            for campaign in campaigns:
                conn.execute(
                    """
                    INSERT INTO fb_campaigns(campaign_id, account_id, name, objective, status, created_time, updated_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(campaign_id) DO UPDATE SET
                        account_id=excluded.account_id,
                        name=excluded.name,
                        objective=excluded.objective,
                        status=excluded.status,
                        created_time=excluded.created_time,
                        updated_at=excluded.updated_at
                    """,
                    (
                        campaign["id"], account_id, campaign.get("name"), campaign.get("objective"),
                        campaign.get("status"), campaign.get("created_time"), updated_at,
                    ),
                )
            result.campaigns = len(campaigns)

            for ad in ads:
                campaign_id = ad.get("campaign", {}).get("id")
                if not campaign_id:
                    continue
                conn.execute(
                    """
                    INSERT INTO fb_ads(ad_id, campaign_id, adset_id, name, status, updated_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(ad_id) DO UPDATE SET
                        campaign_id=excluded.campaign_id,
                        adset_id=excluded.adset_id,
                        name=excluded.name,
                        status=excluded.status,
                        updated_at=excluded.updated_at
                    """,
                    (ad["id"], campaign_id, ad.get("adset_id"), ad.get("name"), ad.get("status"), updated_at),
                )

                creative = ad.get("creative")
                if not creative:
                    continue
                conn.execute(
                    """
                    INSERT INTO fb_creatives(
                        creative_id, ad_id, title, body, image_url, thumbnail_url,
                        video_id, instagram_user_id, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(creative_id) DO UPDATE SET
                        ad_id=excluded.ad_id,
                        title=excluded.title,
                        body=excluded.body,
                        image_url=excluded.image_url,
                        thumbnail_url=excluded.thumbnail_url,
                        video_id=excluded.video_id,
                        instagram_user_id=excluded.instagram_user_id,
                        updated_at=excluded.updated_at
                    """,
                    (
                        creative["id"], ad["id"], creative.get("title"), creative.get("body"),
                        creative.get("image_url"), creative.get("thumbnail_url"), creative.get("video_id"),
                        creative.get("instagram_user_id"), updated_at,
                    ),
                )
                result.creatives += 1
            result.ads = len(ads)

            for row in insights:
                conn.execute(
                    """
                    INSERT INTO fb_insights(
                        ad_id, date_start, date_stop, spend, impressions, reach, clicks, instagram_followers
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(ad_id, date_start, date_stop) DO UPDATE SET
                        spend=excluded.spend,
                        impressions=excluded.impressions,
                        reach=excluded.reach,
                        clicks=excluded.clicks,
                        instagram_followers=excluded.instagram_followers
                    """,
                    (
                        row["ad_id"], row["date_start"], row["date_stop"],
                        float(row.get("spend", 0) or 0), int(row.get("impressions", 0) or 0),
                        int(row.get("reach", 0) or 0), int(row.get("clicks", 0) or 0),
                        _instagram_followers_from_insight(row),
                    ),
                )
            result.insight_rows = len(insights)

            try:
                instagram_accounts = get_instagram_accounts(account_id)
                for instagram_account in instagram_accounts:
                    if not instagram_account.get("id"):
                        continue
                    conn.execute(
                        """
                        INSERT INTO fb_instagram_accounts(account_id, instagram_user_id, username, updated_at)
                        VALUES(?,?,?,?)
                        ON CONFLICT(account_id, instagram_user_id) DO UPDATE SET
                            username=excluded.username,
                            updated_at=excluded.updated_at
                        """,
                        (account_id, instagram_account["id"], instagram_account.get("username"), updated_at),
                    )
            except FacebookApiError:
                # Account discovery is optional: insights should still be saved when
                # the token cannot list connected Instagram profiles.
                pass

            conn.commit()
            result.status = "ok"
            result.message = (
                f"{result.campaigns} campaigns, {result.ads} ads, "
                f"{result.creatives} creatives, {result.insight_rows} insight rows"
            )
            follow_action_types = sorted({
                str(action.get("action_type"))
                for row in insights
                for field_name in ("actions", "conversions")
                for action in (row.get(field_name) or [])
                if "follow" in str(action.get("action_type", "")).lower()
                or str(action.get("action_type", "")).lower() == "like"
            })
            result.message += (
                f"; follow action types: {', '.join(follow_action_types)}"
                if follow_action_types
                else "; Meta returned no follow action_type"
            )
            if profile_name:
                result.message = f"@{profile_name}: {result.message}"
            _log_finish(conn, log_id, "ok", result.message)
        except FacebookApiError as exc:
            conn.rollback()
            result.status = "error"
            result.message = str(exc)
            _log_finish(conn, log_id, "error", result.message)
        except Exception as exc:
            conn.rollback()
            result.status = "error"
            result.message = f"Unexpected error: {exc}"
            _log_finish(conn, log_id, "error", result.message)
        finally:
            _release_sync_lock(account_id)
    return result


def sync_all_active_accounts(since: date, until: date) -> list[SyncResult]:
    with connect_db() as conn:
        account_ids = [
            row[0]
            for row in conn.execute("SELECT account_id FROM fb_ad_accounts WHERE is_active=1").fetchall()
        ]
    return [sync_ad_account(account_id, since, until, triggered_by="system") for account_id in account_ids]


def last_sync_for_account(account_id: str) -> Optional[dict]:
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT started_at, finished_at, status, message, triggered_by, period_start, period_end
            FROM fb_sync_log
            WHERE account_id=?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        return dict(row) if row else None
