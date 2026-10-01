from __future__ import annotations

from datetime import date
from typing import Optional

from core.auth import require_permission
from core.config import ACCOUNT_ALIASES, now_utc
from core.csv_import import clean_id, is_novakid_account, recalc_final
from core.database import connect_db
from core.i18n import tr
from integrations.ad_naming import profile_from_ad_names
from integrations.facebook_ads_client import is_configured
from integrations.facebook_ads_sync import MAX_SYNC_DAYS, sync_ad_account

API_SOURCE_NAME = "Facebook Ads API"


def _normalize_account(value: object) -> Optional[str]:
    account = str(value or "").strip().lstrip("@").lower()
    if not account:
        return None
    return ACCOUNT_ALIASES.get(account, account)


def resolve_paid_rows(
    ads: list[dict],
    meta_accounts_by_id: dict[str, set[str]],
    media_accounts: dict[str, str],
    target_accounts: set[str],
) -> tuple[dict[tuple[str, str], dict], list[dict]]:
    """Aggregate ad insights into paid rows keyed like the old PR upload.

    The ad name is the publication ID by team convention, so it is tried first
    (exactly as the PR file was matched); the post promoted by the creative is
    the fallback. An ad matching no Meta publication stays paid-only under its
    name. Returns the rows and the ads whose Instagram account is unknown.
    """
    rows: dict[tuple[str, str], dict] = {}
    unresolved: list[dict] = []
    for ad in ads:
        followers = float(ad.get("followers") or 0)
        spend = float(ad.get("spend") or 0)
        if not followers and not spend:
            continue
        name_id = clean_id(ad.get("ad_name"))
        media_id = clean_id(ad.get("effective_instagram_media_id"))
        publication_id: Optional[str] = None
        account: Optional[str] = None
        for candidate in (name_id, media_id):
            accounts = meta_accounts_by_id.get(candidate) if candidate else None
            if accounts and len(accounts) == 1:
                publication_id, account = candidate, next(iter(accounts))
                break
        if publication_id is None:
            publication_id = name_id or media_id
            account = (
                media_accounts.get(media_id)
                or _normalize_account(ad.get("instagram_username"))
                or profile_from_ad_names(ad.get("campaign_name"), ad.get("ad_name"))
            )
        account = _normalize_account(account)
        if not publication_id or not account or not is_novakid_account(account):
            unresolved.append(ad)
            continue
        if account not in target_accounts:
            continue
        row = rows.setdefault((account, publication_id), {"followers": 0.0, "spend": 0.0})
        row["followers"] += followers
        row["spend"] += spend
    return rows, unresolved


def _period_ads(conn, account_ids: list[str], period_start: str, period_end: str) -> list[dict]:
    placeholders = ",".join(["?"] * len(account_ids))
    cursor = conn.execute(
        f"""
        SELECT a.ad_id, a.name AS ad_name, c.name AS campaign_name,
               cr.effective_instagram_media_id, ia.username AS instagram_username,
               SUM(i.instagram_followers) AS followers, SUM(i.spend) AS spend
        FROM fb_insights i
        JOIN fb_ads a ON a.ad_id = i.ad_id
        JOIN fb_campaigns c ON c.campaign_id = a.campaign_id
        LEFT JOIN fb_creatives cr ON cr.ad_id = a.ad_id
        LEFT JOIN fb_instagram_accounts ia
            ON ia.account_id = c.account_id
           AND ia.instagram_user_id = cr.instagram_user_id
        WHERE c.account_id IN ({placeholders})
          AND i.date_start >= ?
          AND i.date_stop <= ?
        GROUP BY a.ad_id, a.name, c.name, cr.effective_instagram_media_id, ia.username
        """,
        (*account_ids, period_start, period_end),
    )
    columns = ["ad_id", "ad_name", "campaign_name", "effective_instagram_media_id", "instagram_username", "followers", "spend"]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def _active_ad_account_ids() -> list[str]:
    with connect_db() as conn:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT account_id FROM fb_ad_accounts WHERE is_active=1"
            ).fetchall()
        ]


def _store_paid_from_synced_ads(
    period_start: str,
    period_end: str,
    accounts: set[str],
    username: str,
    ad_account_ids: list[str],
) -> tuple[bool, list[str]]:
    """Materialize paid rows from already stored fb_* tables without an API call."""
    with connect_db() as conn:
        period_ads = _period_ads(conn, ad_account_ids, period_start, period_end)
        if not period_ads:
            return False, [tr(
                "No synchronized Ads data was found for this period. Existing paid followers were left unchanged.",
                "За этот период не найдены синхронизированные данные Ads. Существующие paid-подписчики не изменены.",
            )]

        meta_accounts_by_id: dict[str, set[str]] = {}
        for publication_id, account in conn.execute(
            "SELECT publication_id, account FROM meta_publications WHERE period_start=? AND period_end=?",
            (period_start, period_end),
        ).fetchall():
            meta_accounts_by_id.setdefault(str(publication_id), set()).add(str(account))
        media_accounts = {
            str(media_id): str(account)
            for media_id, account in conn.execute("SELECT media_id, account FROM ig_media").fetchall()
        }
        paid_rows, unresolved = resolve_paid_rows(
            period_ads, meta_accounts_by_id, media_accounts, accounts,
        )

        warnings: list[str] = []
        unresolved_followers = int(round(sum(float(ad.get("followers") or 0) for ad in unresolved)))
        if unresolved:
            names = ", ".join(str(ad.get("ad_name") or ad.get("ad_id")) for ad in unresolved[:10])
            warnings.append(tr(
                f"Ads without a recognizable Instagram account were skipped ({len(unresolved)}, {unresolved_followers:,} followers): {names}.",
                f"Пропущены объявления без определённого Instagram-аккаунта ({len(unresolved)}, {unresolved_followers:,} подписчиков): {names}.",
            ))

        uploaded_at = now_utc()
        for account in sorted(accounts):
            conn.execute(
                "DELETE FROM pr_ads WHERE account=? AND period_start=? AND period_end=?",
                (account, period_start, period_end),
            )
        conn.executemany(
            """
            INSERT INTO pr_ads(
                account, period_start, period_end, month, publication_id, pr_followers, spend_usd,
                pr_filename, uploaded_by, uploaded_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    account, period_start, period_end, period_start[:7], publication_id,
                    int(round(values["followers"])), round(values["spend"], 2), API_SOURCE_NAME, username, uploaded_at,
                )
                for (account, publication_id), values in sorted(paid_rows.items())
            ],
        )
        paid_total = int(sum(round(values["followers"]) for values in paid_rows.values()))
        conn.execute(
            "INSERT INTO uploads(file_type,account,period_start,period_end,filename,stored_path,uploaded_by,uploaded_at,rows_saved,warnings) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("pr", "auto", period_start, period_end, API_SOURCE_NAME, None, username, uploaded_at, len(paid_rows), "\n".join(warnings)),
        )
        conn.commit()
    warnings.insert(0, tr(
        f"Paid from the ad account for {period_start} – {period_end}: {len(paid_rows)} ads, {paid_total:,} followers.",
        f"Paid из рекламного кабинета за {period_start} – {period_end}: {len(paid_rows)} объявлений, {paid_total:,} подписчиков.",
    ))
    return True, warnings


def save_paid_from_synced_data(
    period_start: str, period_end: str, accounts: set[str], username: str
) -> tuple[bool, list[str]]:
    """Build paid rows from a completed sync without calling Meta again."""
    if not accounts:
        return False, []
    ad_account_ids = _active_ad_account_ids()
    if not ad_account_ids:
        return False, [tr(
            "No active Facebook ad accounts: add one on the Facebook Ads page to use synchronized data.",
            "Нет активных рекламных кабинетов: добавьте кабинет на странице Facebook Ads, чтобы использовать синхронизированные данные.",
        )]
    with connect_db() as conn:
        missing_syncs = [
            account_id
            for account_id in ad_account_ids
            if conn.execute(
                """
                SELECT 1 FROM fb_sync_log
                WHERE account_id=? AND status='ok'
                  AND period_start<=? AND period_end>=?
                ORDER BY finished_at DESC LIMIT 1
                """,
                (account_id, period_start, period_end),
            ).fetchone() is None
        ]
    if missing_syncs:
        return False, [tr(
            "No successful sync covering this period was found for: " + ", ".join(missing_syncs) + ". Existing paid followers were left unchanged.",
            "Не найдена успешная синхронизация за весь период для: " + ", ".join(missing_syncs) + ". Существующие paid-подписчики не изменены.",
        )]
    return _store_paid_from_synced_ads(
        period_start, period_end, accounts, username, ad_account_ids,
    )


def save_paid_from_ads_api(
    period_start: str, period_end: str, accounts: set[str], username: str
) -> tuple[bool, list[str]]:
    """Sync ad accounts for the period and replace its paid rows with API data.

    Nothing is written unless every active ad account synced successfully, so a
    failed request never wipes paid followers that were already saved.
    """
    if not accounts:
        return False, []
    if not is_configured():
        return False, [tr(
            "META_ACCESS_TOKEN is not configured: paid followers were not loaded from the ad account.",
            "META_ACCESS_TOKEN не настроен: paid-подписчики не загружены из рекламного кабинета.",
        )]
    since, until = date.fromisoformat(period_start), date.fromisoformat(period_end)
    if (until - since).days + 1 > MAX_SYNC_DAYS:
        return False, [tr(
            f"The period is longer than {MAX_SYNC_DAYS} days: paid followers were not loaded from the ad account.",
            f"Период длиннее {MAX_SYNC_DAYS} дней: paid-подписчики не загружены из рекламного кабинета.",
        )]
    ad_account_ids = _active_ad_account_ids()
    if not ad_account_ids:
        return False, [tr(
            "No active Facebook ad accounts: add one on the Facebook Ads page to load paid followers.",
            "Нет активных рекламных кабинетов: добавьте кабинет на странице Facebook Ads, чтобы загружать paid-подписчиков.",
        )]

    for ad_account_id in ad_account_ids:
        result = sync_ad_account(ad_account_id, since, until, triggered_by=username, enforce_cooldown=False)
        if result.status != "ok":
            return False, [tr(
                f"Ad account {ad_account_id} did not sync ({result.message}). Paid followers for this period were left unchanged.",
                f"Кабинет {ad_account_id} не синхронизировался ({result.message}). Paid-подписчики за период не изменены.",
            )]

    return _store_paid_from_synced_ads(
        period_start, period_end, accounts, username, ad_account_ids,
    )


def refresh_paid_from_ads_api(period_start: str, period_end: str, user: dict) -> list[str]:
    """Reload paid followers for an already uploaded Meta period and recalculate it."""
    require_permission(user, "upload_meta")
    with connect_db() as conn:
        accounts = {
            str(row[0]) for row in conn.execute(
                "SELECT DISTINCT account FROM meta_publications WHERE period_start=? AND period_end=?",
                (period_start, period_end),
            ).fetchall()
        }
    saved, warnings = save_paid_from_ads_api(period_start, period_end, accounts, user["username"])
    if saved:
        for account in sorted(accounts):
            recalc_final(account, period_start, period_end)
    return warnings


def refresh_paid_from_synced_data(
    period_start: str, period_end: str, user: dict
) -> tuple[bool, list[str]]:
    """Finish matching and recalculation using an already successful Ads sync."""
    require_permission(user, "upload_meta")
    with connect_db() as conn:
        accounts = {
            str(row[0]) for row in conn.execute(
                "SELECT DISTINCT account FROM meta_publications WHERE period_start=? AND period_end=?",
                (period_start, period_end),
            ).fetchall()
        }
    saved, messages = save_paid_from_synced_data(
        period_start, period_end, accounts, user["username"],
    )
    if saved:
        for account in sorted(accounts):
            recalc_final(account, period_start, period_end)
    return saved, messages
