from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import plotly.express as px
import streamlit as st

from core.auth import has_permission
from core.csv_import import dataframe_to_excel_bytes
from core.db import db_df
from core.i18n import tr
from core.style import MARK_COLORS, hero
from integrations.facebook_ads_client import FacebookApiError, is_configured
from integrations.facebook_ads_sync import last_sync_for_account
from integrations.instagram_sync import LOCK_PREFIX, MAX_SYNC_DAYS, discover_accounts, sync_instagram_account
from screens._shared import apply_date_filter, shared_results_filters

SHORTCODE_RE = re.compile(r"instagram\.com/(?:[^/?#]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE)
SOURCE_BOTH = "api+export"
SOURCE_API = "api"
SOURCE_EXPORT = "export"
POST_COLUMNS = [
    "account", "published_at", "month", "format", "caption", "preview_url", "permalink",
    "reach", "views", "like_count", "comments_count", "saved", "shares", "total_interactions",
    "engagement_rate", "api_follows", "profile_visits",
    "followers_total", "followers_paid", "followers_organic", "organic_per_1k_reach",
    "source", "publication_id", "insights_error",
]


def _shortcode(url: object) -> Optional[str]:
    match = SHORTCODE_RE.search(str(url or ""))
    return match.group(1) if match else None


def _format_label(media_product_type: object, media_type: object, permalink: object = None) -> str:
    if media_product_type == "REELS" or "/reel" in str(permalink or ""):
        return "Reels"
    if media_type == "CAROUSEL_ALBUM":
        return "Carousel"
    if media_type == "VIDEO":
        return "Video"
    if media_type == "IMAGE":
        return "Photo"
    return "Post"


def _number(value: object) -> Optional[float]:
    return None if value is None or pd.isna(value) else float(value)


def _latest_follower_rows(results: pd.DataFrame) -> pd.DataFrame:
    """One calculated row per publication, newest report wins (as on the Dashboard)."""
    if results.empty:
        return results
    return (
        results.sort_values(["period_end", "updated_at"], na_position="first")
        .drop_duplicates(["publication_id"], keep="last")
    )


def merge_posts_with_followers(api_posts: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    """Join API post statistics with followers calculated from Meta/PR uploads.

    Posts match by Instagram media ID (the Meta export's publication ID) and fall
    back to the permalink shortcode. Export posts missing from the API are kept
    so the table still covers everything that was uploaded.
    """
    followers = _latest_follower_rows(results)
    by_id: dict[str, dict] = {}
    by_code: dict[str, dict] = {}
    for row in followers.to_dict("records"):
        by_id[str(row["publication_id"])] = row
        code = _shortcode(row.get("publication_link"))
        if code:
            by_code.setdefault(code, row)

    rows: list[dict] = []
    matched_ids: set[str] = set()
    for post in api_posts.to_dict("records"):
        match = by_id.get(str(post["media_id"])) or by_code.get(_shortcode(post.get("permalink")) or "")
        if match:
            matched_ids.add(str(match["publication_id"]))
        reach = _number(post.get("reach"))
        if reach is None and match:
            reach = _number(match.get("post_reach"))
        rows.append({
            "account": post["account"],
            "published_at": post.get("published_at"),
            "month": post.get("month"),
            "format": _format_label(post.get("media_product_type"), post.get("media_type"), post.get("permalink")),
            "caption": post.get("caption"),
            "preview_url": post.get("preview_url"),
            "permalink": post.get("permalink"),
            "reach": reach,
            "views": _number(post.get("views")),
            "like_count": _number(post.get("like_count")),
            "comments_count": _number(post.get("comments_count")),
            "saved": _number(post.get("saved")),
            "shares": _number(post.get("shares")),
            "total_interactions": _number(post.get("total_interactions")),
            "api_follows": _number(post.get("follows")),
            "profile_visits": _number(post.get("profile_visits")),
            "followers_total": _number(match.get("meta_followers")) if match else None,
            "followers_paid": _number(match.get("pr_followers")) if match else None,
            "followers_organic": _number(match.get("final_followers")) if match else None,
            "source": SOURCE_BOTH if match else SOURCE_API,
            "publication_id": str(post["media_id"]),
            "insights_error": post.get("insights_error"),
        })

    for row in followers.to_dict("records"):
        # Paid-only PR rows have no Meta publication, so they are not posts.
        if str(row["publication_id"]) in matched_ids or pd.isna(row.get("meta_uploaded_by")):
            continue
        published_at = row.get("publication_date")
        published_at = None if published_at is None or pd.isna(published_at) else str(published_at)
        rows.append({
            "account": row["account"],
            "published_at": published_at,
            "month": published_at[:7] if published_at else str(row["month"])[:7],
            "format": _format_label(None, None, row.get("publication_link")),
            "permalink": row.get("publication_link"),
            "reach": _number(row.get("post_reach")),
            "followers_total": _number(row.get("meta_followers")),
            "followers_paid": _number(row.get("pr_followers")),
            "followers_organic": _number(row.get("final_followers")),
            "source": SOURCE_EXPORT,
            "publication_id": str(row["publication_id"]),
        })

    merged = pd.DataFrame(rows, columns=POST_COLUMNS)
    if merged.empty:
        return merged
    reach = merged["reach"].where(merged["reach"] > 0)
    merged["engagement_rate"] = merged["total_interactions"] / reach * 100
    merged["organic_per_1k_reach"] = merged["followers_organic"] / reach * 1000
    return merged.sort_values("published_at", ascending=False, na_position="last").reset_index(drop=True)


def format_summary(posts: pd.DataFrame) -> pd.DataFrame:
    summary = posts.groupby("format", as_index=False).agg(
        posts=("publication_id", "count"),
        reach=("reach", "sum"),
        followers_organic=("followers_organic", "sum"),
    )
    summary["organic_per_1k_reach"] = summary["followers_organic"] / summary["reach"].where(summary["reach"] > 0) * 1000
    return summary.sort_values("organic_per_1k_reach", ascending=False, na_position="last")


def _last_sync_label(ig_user_id: str) -> str:
    last_sync = last_sync_for_account(f"{LOCK_PREFIX}{ig_user_id}")
    if not last_sync:
        return tr("not synced yet", "ещё не синхронизировано")
    return f"{last_sync['status']} — {last_sync['finished_at'] or last_sync['started_at']}"


def _render_sync_panel(user: dict) -> None:
    accounts = db_df("SELECT * FROM ig_accounts WHERE is_active=1 ORDER BY username")
    with st.expander(tr("Instagram API sync", "Синхронизация с Instagram API"), expanded=accounts.empty):
        if not is_configured():
            st.warning(tr(
                "META_ACCESS_TOKEN is not configured, so posts cannot be loaded from the API.",
                "META_ACCESS_TOKEN не настроен, поэтому посты нельзя загрузить через API.",
            ))
            return
        st.caption(tr(
            "The token needs instagram_basic, instagram_manage_insights, pages_show_list and pages_read_engagement, and the system user must be assigned the Novakid Facebook Pages.",
            "Токену нужны права instagram_basic, instagram_manage_insights, pages_show_list и pages_read_engagement, а системному пользователю должны быть назначены Facebook-страницы Novakid.",
        ))
        if st.button(tr("Find Instagram accounts", "Найти Instagram-аккаунты"), use_container_width=True):
            try:
                with st.spinner(tr("Looking for accounts...", "Ищем аккаунты...")):
                    found = discover_accounts()
                st.success(tr(f"Found Novakid accounts: {len(found)}", f"Найдено аккаунтов Novakid: {len(found)}"))
                st.rerun()
            except FacebookApiError as exc:
                st.error(str(exc))

        if accounts.empty:
            st.info(tr(
                "No Instagram accounts yet. Click “Find Instagram accounts”.",
                "Instagram-аккаунтов пока нет. Нажмите «Найти Instagram-аккаунты».",
            ))
            return

        status = accounts[["ig_user_id", "username", "followers_count", "media_count"]].copy()
        status["last_sync"] = status["ig_user_id"].map(_last_sync_label)
        st.dataframe(
            status.drop(columns="ig_user_id"),
            use_container_width=True,
            hide_index=True,
            column_config={
                "username": "Instagram",
                "followers_count": st.column_config.NumberColumn(tr("Followers now", "Подписчиков сейчас"), format="%d"),
                "media_count": st.column_config.NumberColumn(tr("Posts", "Постов"), format="%d"),
                "last_sync": tr("Last sync", "Последняя синхронизация"),
            },
        )

        c1, c2 = st.columns(2)
        until = c2.date_input(tr("Published to", "Опубликовано по"), value=date.today(), key="ig_sync_until")
        since = c1.date_input(tr("Published from", "Опубликовано с"), value=date.today() - timedelta(days=89), key="ig_sync_since")
        usernames = accounts["username"].tolist()
        selected = st.multiselect(tr("Accounts", "Аккаунты"), usernames, default=usernames, key="ig_sync_accounts")
        period_error = ""
        if since > until:
            period_error = tr("The start date must not be later than the end date.", "Дата начала не может быть позже даты окончания.")
        elif (until - since).days + 1 > MAX_SYNC_DAYS:
            period_error = tr(f"Select no more than {MAX_SYNC_DAYS} days.", f"Выберите период не более {MAX_SYNC_DAYS} дней.")
        if period_error:
            st.error(period_error)
        if st.button(tr("Load posts statistics", "Загрузить статистику постов"), type="primary", use_container_width=True):
            if period_error or not selected:
                st.error(period_error or tr("Choose at least one account.", "Выберите хотя бы один аккаунт."))
                return
            progress = st.progress(0.0)
            rows = accounts[accounts["username"].isin(selected)].to_dict("records")
            for index, account in enumerate(rows, start=1):
                with st.spinner(f"@{account['username']}..."):
                    result = sync_instagram_account(
                        account["ig_user_id"], account["username"], since, until, triggered_by=user["username"]
                    )
                if result.status == "ok":
                    st.success(result.message)
                elif result.status == "skipped":
                    st.warning(result.message)
                else:
                    st.error(result.message)
                progress.progress(index / len(rows))


def page_organic_posts(user: dict) -> None:
    if not has_permission(user, "view_dashboard"):
        st.error(tr("You do not have permission to view this page.", "У вас нет прав для просмотра этой страницы."))
        return

    hero(
        tr("Organic posts", "Органика: посты"),
        tr(
            "Statistics of every Instagram post from the API next to total, paid and organic followers calculated from Meta and PR uploads.",
            "Статистика всех постов Instagram из API рядом с total, paid и organic подписчиками, рассчитанными из выгрузок Meta и PR.",
        ),
        ["Reach", "Engagement", "Followers organic", "Per 1k reach"],
    )
    if has_permission(user, "sync_instagram"):
        _render_sync_panel(user)

    posts = merge_posts_with_followers(
        db_df("SELECT * FROM ig_media"),
        db_df("SELECT * FROM final_results"),
    )
    if posts.empty:
        st.info(tr(
            "No posts yet. Load statistics from the Instagram API or upload a Meta export.",
            "Постов пока нет. Загрузите статистику из Instagram API или выгрузку Meta.",
        ))
        return

    selected_accounts, selected_periods, _ = shared_results_filters(posts.dropna(subset=["month"]), show_warnings=False)
    f = posts[posts["account"].isin(selected_accounts)] if selected_accounts else posts.iloc[0:0]
    f = apply_date_filter(f, selected_periods)
    if f.empty:
        st.info(tr("Choose at least one region and period.", "Выберите хотя бы один регион и период."))
        return

    c1, c2 = st.columns([2, 1])
    formats = sorted(f["format"].unique().tolist())
    selected_formats = c1.multiselect(tr("Format", "Формат"), formats, default=formats, key="organic_posts_formats")
    only_calculated = c2.checkbox(
        tr("Only posts with calculated followers", "Только посты с рассчитанными подписчиками"),
        key="organic_posts_only_calculated",
    )
    f = f[f["format"].isin(selected_formats)]
    if only_calculated:
        f = f[f["followers_organic"].notna()]
    if f.empty:
        st.info(tr("No posts match the selected filters.", "Нет постов под выбранные фильтры."))
        return

    reach = f["reach"].sum()
    organic = f["followers_organic"].sum()
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric(tr("Posts", "Постов"), f"{len(f):,}")
    m2.metric("Reach", f"{int(reach):,}")
    m3.metric(tr("Avg ER", "Средний ER"), f"{f['engagement_rate'].mean():.1f}%" if f["engagement_rate"].notna().any() else "—")
    m4.metric("Followers organic", f"{int(organic):,}")
    m5.metric(tr("Organic per 1k reach", "Organic на 1k охвата"), f"{organic / reach * 1000:.2f}" if reach else "—")

    unmatched_api = int((f["source"] == SOURCE_API).sum())
    export_only = int((f["source"] == SOURCE_EXPORT).sum())
    if unmatched_api or export_only:
        st.caption(tr(
            f"{unmatched_api} API posts have no calculated followers yet (no Meta export for them); {export_only} export posts are not in the API sync.",
            f"У {unmatched_api} постов из API ещё нет рассчитанных подписчиков (нет выгрузки Meta); {export_only} постов из выгрузок нет в синхронизации API.",
        ))

    summary = format_summary(f)
    chart_col, table_col = st.columns([1.2, 1])
    with chart_col:
        fig = px.bar(
            summary, x="format", y="organic_per_1k_reach", color="format", color_discrete_sequence=MARK_COLORS,
            title=tr("Organic followers per 1k reach by format", "Organic подписчики на 1k охвата по форматам"),
            labels={"format": tr("Format", "Формат"), "organic_per_1k_reach": tr("Per 1k reach", "На 1k охвата")},
        )
        fig.update_layout(
            template="plotly_white", showlegend=False, font_family="Onest", title_font_family="Unbounded",
            xaxis_title="", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, use_container_width=True)
    with table_col:
        st.dataframe(summary, use_container_width=True, hide_index=True, column_config={
            "format": tr("Format", "Формат"),
            "posts": tr("Posts", "Постов"),
            "reach": st.column_config.NumberColumn("Reach", format="%d"),
            "followers_organic": st.column_config.NumberColumn("Followers organic", format="%d"),
            "organic_per_1k_reach": st.column_config.NumberColumn(tr("Per 1k reach", "На 1k охвата"), format="%.2f"),
        })

    st.markdown("### " + tr("All posts", "Все посты"))
    source_labels = {
        SOURCE_BOTH: tr("API + export", "API + выгрузка"),
        SOURCE_API: "API",
        SOURCE_EXPORT: tr("Export only", "Только выгрузка"),
    }
    table = f.assign(source=f["source"].map(source_labels))
    st.dataframe(
        table.drop(columns=["month", "publication_id"]),
        use_container_width=True,
        hide_index=True,
        column_config={
            "account": tr("Region", "Регион"),
            "published_at": tr("Published", "Опубликован"),
            "format": tr("Format", "Формат"),
            "caption": st.column_config.TextColumn(tr("Caption", "Подпись"), width="medium"),
            "preview_url": st.column_config.ImageColumn(tr("Preview", "Превью")),
            "permalink": st.column_config.LinkColumn(tr("Link", "Ссылка"), display_text="Open"),
            "reach": st.column_config.NumberColumn("Reach", format="%d"),
            "views": st.column_config.NumberColumn("Views", format="%d"),
            "like_count": st.column_config.NumberColumn("Likes", format="%d"),
            "comments_count": st.column_config.NumberColumn(tr("Comments", "Комментарии"), format="%d"),
            "saved": st.column_config.NumberColumn(tr("Saves", "Сохранения"), format="%d"),
            "shares": st.column_config.NumberColumn(tr("Shares", "Репосты"), format="%d"),
            "total_interactions": st.column_config.NumberColumn(tr("Interactions", "Взаимодействия"), format="%d"),
            "engagement_rate": st.column_config.NumberColumn("ER, %", format="%.2f%%"),
            "api_follows": st.column_config.NumberColumn(
                "Follows (API)", format="%d",
                help=tr("Follows reported by the Instagram API. Not available for Reels.", "Подписки по данным Instagram API. Для Reels недоступны."),
            ),
            "profile_visits": st.column_config.NumberColumn(tr("Profile visits", "Переходы в профиль"), format="%d"),
            "followers_total": st.column_config.NumberColumn("Followers total", format="%d"),
            "followers_paid": st.column_config.NumberColumn("Followers paid", format="%d"),
            "followers_organic": st.column_config.NumberColumn(
                "Followers organic", format="%d",
                help=tr("Calculated from uploads: Meta follows minus PR paid.", "Рассчитано из выгрузок: подписки Meta минус paid из PR."),
            ),
            "organic_per_1k_reach": st.column_config.NumberColumn(tr("Organic / 1k reach", "Organic / 1k охвата"), format="%.2f"),
            "source": tr("Source", "Источник"),
            "insights_error": tr("API note", "Примечание API"),
        },
    )

    if has_permission(user, "export_reports"):
        export = table.drop(columns=["preview_url"])
        d1, d2 = st.columns(2)
        d1.download_button(
            "CSV", export.to_csv(index=False).encode("utf-8-sig"), "organic_posts.csv", "text/csv", use_container_width=True
        )
        d2.download_button(
            "Excel", dataframe_to_excel_bytes(export), "organic_posts.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True,
        )
