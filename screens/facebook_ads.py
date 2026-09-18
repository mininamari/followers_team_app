from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core.auth import has_permission
from core.config import now_utc
from core.database import connect_db
from core.db import db_df
from core.i18n import tr
from core.style import hero
from integrations.facebook_ads_client import is_configured
from integrations.facebook_ads_sync import MAX_SYNC_DAYS, last_sync_for_account, sync_ad_account

FATIGUE_MIN_DAYS = 6
FATIGUE_DECLINE_RATIO = 0.7
MIN_ROWS_FOR_PERFORMANCE_FLAG = 4


def _save_ad_account(account_id: str, label: str, is_active: bool) -> None:
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO fb_ad_accounts(account_id, label, novakid_account, is_active, created_at)
            VALUES(?,?,NULL,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                label=excluded.label,
                is_active=excluded.is_active
            """,
            (account_id, label, int(is_active), now_utc()),
        )
        conn.commit()


def _save_creative_tags(df: pd.DataFrame) -> None:
    with connect_db() as conn:
        for _, row in df.iterrows():
            conn.execute(
                "UPDATE fb_creatives SET tags=? WHERE creative_id=?",
                (row["tags"] if pd.notna(row["tags"]) and str(row["tags"]).strip() else None, row["creative_id"]),
            )
        conn.commit()


def _ads_overview_df(
    account_ids: list[str], since: date, until: date, instagram_user_id: str | None = None
) -> pd.DataFrame:
    if not account_ids:
        return pd.DataFrame()
    placeholders = ",".join(["?"] * len(account_ids))
    query = f"""
        SELECT
            c.account_id,
            c.campaign_id,
            c.name AS campaign_name,
            c.objective,
            c.status AS campaign_status,
            a.ad_id,
            a.name AS ad_name,
            a.status AS ad_status,
            cr.creative_id,
            cr.title,
            cr.thumbnail_url,
            cr.tags,
            cr.instagram_user_id,
            ia.username AS instagram_username,
            COALESCE(SUM(i.spend), 0) AS spend,
            COALESCE(SUM(i.impressions), 0) AS impressions,
            COALESCE(SUM(i.reach), 0) AS reach,
            COALESCE(SUM(i.clicks), 0) AS clicks
        FROM fb_campaigns c
        JOIN fb_ads a ON a.campaign_id = c.campaign_id
        LEFT JOIN fb_creatives cr ON cr.ad_id = a.ad_id
        LEFT JOIN fb_instagram_accounts ia
            ON ia.account_id = c.account_id
           AND ia.instagram_user_id = cr.instagram_user_id
        LEFT JOIN fb_insights i ON i.ad_id = a.ad_id
        WHERE c.account_id IN ({placeholders})
          AND i.date_start >= ?
          AND i.date_stop <= ?
          {"AND cr.instagram_user_id = ?" if instagram_user_id else ""}
        GROUP BY
            c.account_id,
            c.campaign_id,
            c.name,
            c.objective,
            c.status,
            a.ad_id,
            a.name,
            a.status,
            cr.creative_id,
            cr.title,
            cr.thumbnail_url,
            cr.tags,
            cr.instagram_user_id,
            ia.username
        ORDER BY spend DESC
    """
    params = [*account_ids, since.isoformat(), until.isoformat()]
    if instagram_user_id:
        params.append(instagram_user_id)
    return db_df(query, params)


def _fatigued_ad_ids(ad_ids: list[str], since: date, until: date) -> set[str]:
    """Ads whose daily reach dropped off in the second half of their history.

    Mirrors the "creative fatigue" idea from Alison.ai's dashboard: a creative that
    used to reach people well but is tailing off, even if it's still spending.
    """
    if not ad_ids:
        return set()
    placeholders = ",".join(["?"] * len(ad_ids))
    daily = db_df(
        f"""SELECT ad_id, date_start, reach FROM fb_insights
            WHERE ad_id IN ({placeholders}) AND date_start >= ? AND date_stop <= ?
            ORDER BY ad_id, date_start""",
        [*ad_ids, since.isoformat(), until.isoformat()],
    )
    if daily.empty:
        return set()
    fatigued: set[str] = set()
    for ad_id, group in daily.groupby("ad_id"):
        reach = group["reach"].tolist()
        if len(reach) < FATIGUE_MIN_DAYS:
            continue
        midpoint = len(reach) // 2
        early_avg = sum(reach[:midpoint]) / midpoint
        recent_avg = sum(reach[midpoint:]) / (len(reach) - midpoint)
        if early_avg > 0 and recent_avg < early_avg * FATIGUE_DECLINE_RATIO:
            fatigued.add(ad_id)
    return fatigued


def _add_performance_flags(df: pd.DataFrame, since: date, until: date) -> pd.DataFrame:
    if df.empty:
        df["ctr"] = None
        df["fatigue"] = ""
        df["performance_flag"] = ""
        return df

    df = df.copy()
    df["ctr"] = df.apply(
        lambda r: round(r["clicks"] / r["impressions"] * 100, 2) if r["impressions"] else None,
        axis=1,
    )

    fatigued = _fatigued_ad_ids(df["ad_id"].dropna().unique().tolist(), since, until)
    df["fatigue"] = df["ad_id"].apply(lambda ad_id: "📉 Fatigue" if ad_id in fatigued else "")

    if len(df) >= MIN_ROWS_FOR_PERFORMANCE_FLAG and df["ctr"].notna().any():
        spend_threshold = df["spend"].quantile(0.75)
        ctr_threshold = df["ctr"].quantile(0.25)

        def _flag(row: pd.Series) -> str:
            if row["spend"] >= spend_threshold and pd.notna(row["ctr"]) and row["ctr"] <= ctr_threshold:
                return "⚠️ High spend, low CTR"
            return ""

        df["performance_flag"] = df.apply(_flag, axis=1)
    else:
        df["performance_flag"] = ""
    return df


def page_facebook_ads(user: dict) -> None:
    if not has_permission(user, "view_fb_ads"):
        st.error(tr("You do not have permission to view Facebook Ads.", "У вас нет прав для просмотра Facebook Ads."))
        return

    hero(
        "Facebook Ads",
        tr(
            "Campaigns, ads, creatives, and performance from the Facebook Marketing API.",
            "Кампании, объявления, креативы и показатели из Facebook Marketing API.",
        ),
        ["Campaigns", "Creatives", "Spend", "API data"],
    )

    if not is_configured():
        st.warning(
            tr(
                "META_ACCESS_TOKEN is not configured. Sync is unavailable until a Facebook Marketing API system user token is created in Business Manager and added to environment variables.",
                "META_ACCESS_TOKEN не настроен. Синхронизация недоступна, пока не будет создан "
                "системный токен Facebook Marketing API (System User token в Business Manager) "
                "и добавлен в переменные окружения.",
            )
        )

    can_manage = has_permission(user, "manage_fb_ads")

    if can_manage:
        with st.expander(tr("Facebook Ad Accounts", "Рекламные аккаунты Facebook"), expanded=False):
            accounts_df = db_df("SELECT * FROM fb_ad_accounts ORDER BY label")
            if not accounts_df.empty:
                st.dataframe(
                    accounts_df[["account_id", "label", "is_active", "created_at"]],
                    use_container_width=True,
                    hide_index=True,
                )
            with st.form("fb_account_mapping"):
                st.caption(
                    tr(
                        "Add the Meta ad account once. Ads for all Instagram accounts inside it will be synchronized together.",
                        "Добавьте рекламный кабинет Meta один раз. Объявления всех Instagram-аккаунтов внутри него синхронизируются вместе.",
                    )
                )
                account_id = st.text_input(tr("Ad account ID", "ID рекламного аккаунта"), placeholder="act_1234567890")
                label = st.text_input(tr("Label", "Название"), placeholder=tr("Main Meta ad account", "Основной кабинет Meta"))
                is_active = st.checkbox(tr("Active", "Активен"), value=True)
                submitted = st.form_submit_button(tr("Save", "Сохранить"), use_container_width=True)
            if submitted:
                if not account_id.strip().startswith("act_"):
                    st.error(tr("Ad account ID must start with act_.", "ID рекламного аккаунта должен начинаться с act_."))
                else:
                    _save_ad_account(account_id.strip(), label.strip(), is_active)
                    st.success(tr("Saved.", "Сохранено."))
                    st.rerun()

    accounts_df = db_df("SELECT * FROM fb_ad_accounts WHERE is_active=1 ORDER BY label")
    if accounts_df.empty:
        st.info(tr("Add a Facebook ad account to start syncing.", "Добавьте рекламный аккаунт Facebook, чтобы начать синхронизацию."))
        return

    st.markdown("### " + tr("Period And Region", "Период и регион"))
    period_col1, period_col2 = st.columns(2)
    default_until = date.today()
    default_since = default_until - timedelta(days=29)
    since = period_col1.date_input(tr("From", "С"), value=default_since, key="fb_ads_since")
    until = period_col2.date_input(tr("To", "По"), value=default_until, key="fb_ads_until")

    period_error = ""
    if since > until:
        period_error = tr("The start date must not be later than the end date.", "Дата начала не может быть позже даты окончания.")
    elif until > date.today():
        period_error = tr("The end date cannot be in the future.", "Дата окончания не может быть в будущем.")
    elif (until - since).days + 1 > MAX_SYNC_DAYS:
        period_error = tr(
            f"Select no more than {MAX_SYNC_DAYS} days.",
            f"Выберите период не более {MAX_SYNC_DAYS} дней.",
        )
    if period_error:
        st.error(period_error)

    account_ids = accounts_df["account_id"].tolist()
    account_placeholders = ",".join(["?"] * len(account_ids))
    instagram_accounts = db_df(
        f"""SELECT instagram_user_id, MAX(username) AS username
            FROM (
                SELECT instagram_user_id, username
                FROM fb_instagram_accounts
                WHERE account_id IN ({account_placeholders})
                UNION ALL
                SELECT cr.instagram_user_id, NULL AS username
                FROM fb_creatives cr
                JOIN fb_ads a ON a.ad_id = cr.ad_id
                JOIN fb_campaigns c ON c.campaign_id = a.campaign_id
                WHERE c.account_id IN ({account_placeholders})
                  AND cr.instagram_user_id IS NOT NULL
            ) profiles
            GROUP BY instagram_user_id
            ORDER BY username, instagram_user_id""",
        [*account_ids, *account_ids],
    )
    region_options: dict[str | None, str] = {None: tr("All Instagram accounts", "Все Instagram-аккаунты")}
    for _, instagram_account in instagram_accounts.iterrows():
        instagram_id = str(instagram_account["instagram_user_id"])
        username = instagram_account.get("username")
        region_options[instagram_id] = f"@{username}" if pd.notna(username) and str(username).strip() else instagram_id
    selected_instagram_id = st.selectbox(
        tr("Instagram account / region", "Instagram-аккаунт / регион"),
        options=list(region_options),
        format_func=lambda value: region_options[value],
        key="fb_ads_instagram_account",
    )
    if instagram_accounts.empty:
        st.caption(
            tr(
                "Run the first sync for the selected period to discover Instagram accounts connected to this ad account.",
                "Запустите первую синхронизацию за выбранный период, чтобы определить Instagram-аккаунты, подключённые к кабинету.",
            )
        )

    if can_manage:
        st.markdown("### " + tr("Sync", "Синхронизация"))
        for _, row in accounts_df.iterrows():
            col1, col2 = st.columns([3, 1])
            last_sync = last_sync_for_account(row["account_id"])
            status_text = (
                tr("not synced yet", "ещё не синхронизировано") if not last_sync else f"{last_sync['status']} — {last_sync['started_at']}"
            )
            if last_sync and last_sync.get("triggered_by"):
                status_text += f" — {tr('started by', 'запустил(а)')}: {last_sync['triggered_by']}"
            col1.write(f"**{row['label'] or row['account_id']}** ({row['account_id']}) — {status_text}")
            if col2.button(
                "Sync now",
                key=f"sync_{row['account_id']}",
                disabled=not is_configured() or bool(period_error),
                use_container_width=True,
            ):
                with st.spinner(tr("Syncing...", "Синхронизация...")):
                    result = sync_ad_account(row["account_id"], since, until, triggered_by=user["username"])
                if result.status == "ok":
                    st.success(result.message)
                elif result.status == "skipped":
                    st.warning(result.message)
                else:
                    st.error(result.message)
                st.rerun()

    st.markdown("### " + tr("Campaigns And Ads", "Кампании и объявления"))
    overview = _ads_overview_df(account_ids, since, until, selected_instagram_id)
    if overview.empty:
        st.info(tr("No data yet. Run sync above.", "Пока нет данных. Запустите синхронизацию выше."))
        return

    overview = _add_performance_flags(overview, since, until)
    overview_full = overview

    sort_options = {
        "Spend": "spend",
        "Reach": "reach",
        "CTR": "ctr",
    }
    filter_col, sort_col = st.columns([2, 1])
    view_filter = filter_col.radio(
        tr("Show", "Показать"),
        [tr("All", "Все"), "Top spend", "Fatigue", "High spend / low CTR"],
        horizontal=True,
    )
    sort_label = sort_col.selectbox(tr("Sort by", "Сортировать по"), list(sort_options.keys()))

    if view_filter == "Top spend":
        overview = overview.sort_values("spend", ascending=False).head(10)
    elif view_filter == "Fatigue":
        overview = overview[overview["fatigue"] != ""]
    elif view_filter == "High spend / low CTR":
        overview = overview[overview["performance_flag"] != ""]

    sort_key = sort_options[sort_label]
    overview = overview.sort_values(sort_key, ascending=False, na_position="last")

    if overview.empty:
        st.info(tr("No ads match the selected filter.", "Нет объявлений под выбранный фильтр."))
    else:
        display_cols = [
            "instagram_username", "campaign_name", "ad_name", "ad_status", "thumbnail_url", "spend", "impressions",
            "reach", "ctr", "fatigue", "performance_flag", "tags",
        ]
        st.dataframe(
            overview[display_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "instagram_username": tr("Instagram / region", "Instagram / регион"),
                "campaign_name": tr("Campaign", "Кампания"),
                "ad_name": tr("Ad", "Объявление"),
                "ad_status": tr("Status", "Статус"),
                "thumbnail_url": st.column_config.ImageColumn("Creative"),
                "spend": st.column_config.NumberColumn("Spend, USD", format="$%.2f"),
                "ctr": st.column_config.NumberColumn("CTR, %", format="%.2f%%"),
                "fatigue": "",
                "performance_flag": "",
                "tags": tr("Tags", "Теги"),
            },
        )

    st.markdown("### " + tr("Creative Tags", "Теги креативов"))
    st.caption(
        tr(
            "Free-form tags, for example carousel, UGC, testimonial, will help select the best creatives for a targeting presentation later.",
            "Свободные теги (например: carousel, UGC, testimonial) — пригодятся позже для отбора "
            "лучших креативов в презентацию для таргетолога.",
        )
    )
    taggable = (
        overview_full[overview_full["creative_id"].notna()][["creative_id", "ad_name", "title", "tags"]]
        .drop_duplicates("creative_id")
    )
    if taggable.empty:
        st.info(tr("No creatives linked to ads yet.", "Пока нет креативов с привязкой к объявлению."))
    else:
        edited_tags = st.data_editor(
            taggable,
            use_container_width=True,
            hide_index=True,
            disabled=["creative_id", "ad_name", "title"],
            column_config={
                "creative_id": None,
                "ad_name": tr("Ad", "Объявление"),
                "title": tr("Creative title", "Заголовок креатива"),
                "tags": st.column_config.TextColumn(tr("Tags", "Теги"), help=tr("Comma separated", "Через запятую")),
            },
            key="fb_creative_tags_editor",
        )
        if st.button(tr("Save tags", "Сохранить теги"), use_container_width=True):
            _save_creative_tags(edited_tags)
            st.success(tr("Tags saved.", "Теги сохранены."))
            st.rerun()
