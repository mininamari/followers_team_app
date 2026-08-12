from __future__ import annotations

from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

from core.auth import has_permission
from core.db import db_df
from core.i18n import tr
from core.style import MARK_COLORS, hero
from screens._shared import apply_date_filter, shared_results_filters

INSTAGRAM_PAGE_HOSTS = ("instagram.com",)
INSTAGRAM_IMAGE_HOSTS = ("instagram.com", "cdninstagram.com", "fbcdn.net")
MAX_PAGE_BYTES = 1_000_000
MAX_PREVIEW_BYTES = 8_000_000


class _OpenGraphImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.image_url: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "meta" or self.image_url:
            return
        values = {str(key).lower(): value for key, value in attrs}
        if values.get("property", "").lower() in {"og:image", "og:image:secure_url"}:
            self.image_url = values.get("content")


def _is_allowed_host(url: str, allowed_suffixes: tuple[str, ...]) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        return parsed.scheme == "https" and any(
            host == suffix or host.endswith(f".{suffix}") for suffix in allowed_suffixes
        )
    except ValueError:
        return False


def _extract_og_image_url(html: str) -> Optional[str]:
    parser = _OpenGraphImageParser()
    parser.feed(html)
    return parser.image_url


def _read_limited(response: requests.Response, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=65_536):
        size += len(chunk)
        if size > maximum_bytes:
            raise ValueError("Response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_get(url: str, allowed_suffixes: tuple[str, ...], **kwargs) -> requests.Response:
    """Follow only HTTPS redirects that stay on explicitly allowed hosts."""
    current_url = url
    for _ in range(4):
        if not _is_allowed_host(current_url, allowed_suffixes):
            raise ValueError("Disallowed preview host")
        response = requests.get(current_url, allow_redirects=False, **kwargs)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise ValueError("Redirect has no destination")
        current_url = urljoin(current_url, location)
    raise ValueError("Too many redirects")


@st.cache_data(ttl=3600, show_spinner=False)
def _load_instagram_preview(publication_url: str) -> Optional[tuple[bytes, str]]:
    """Best-effort public preview fetch; failure never blocks the dashboard."""
    if not _is_allowed_host(publication_url, INSTAGRAM_PAGE_HOSTS):
        return None
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MARK01Preview/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        page = _safe_get(
            publication_url, INSTAGRAM_PAGE_HOSTS, headers=headers, timeout=(3, 8), stream=True
        )
        page.raise_for_status()
        if not _is_allowed_host(page.url, INSTAGRAM_PAGE_HOSTS):
            return None
        page_type = page.headers.get("Content-Type", "").lower()
        if "text/html" not in page_type:
            return None
        page_html = _read_limited(page, MAX_PAGE_BYTES).decode(page.encoding or "utf-8", errors="replace")
        image_url = _extract_og_image_url(page_html)
        if not image_url or not _is_allowed_host(image_url, INSTAGRAM_IMAGE_HOSTS):
            return None

        image = _safe_get(
            image_url,
            INSTAGRAM_IMAGE_HOSTS,
            headers={"User-Agent": headers["User-Agent"]},
            timeout=(3, 10),
            stream=True,
        )
        image.raise_for_status()
        if not _is_allowed_host(image.url, INSTAGRAM_IMAGE_HOSTS):
            return None
        image_type = image.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if image_type not in {"image/jpeg", "image/png", "image/webp"}:
            return None
        return _read_limited(image, MAX_PREVIEW_BYTES), image_type
    except (requests.RequestException, UnicodeError, ValueError):
        return None


def _chart_layout(fig, y_title: str) -> None:
    fig.update_layout(
        template="plotly_white", title_font_size=18, title_font_family="Unbounded",
        font_family="Onest", font_color="#171715", legend_title_text=tr("Region", "Регион"),
        xaxis_title="", yaxis_title=y_title, bargap=0.25,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )


def _top_organic_publications(
    publications: pd.DataFrame,
    selected_accounts: list[str],
    selected_periods: list[str],
    limit: int = 5,
) -> pd.DataFrame:
    """Return unique linked Meta publications ranked within dashboard filters."""
    if publications.empty or not selected_accounts or not selected_periods:
        return publications.iloc[0:0]

    ranked = publications[
        publications["account"].isin(selected_accounts)
        & publications["month"].astype(str).str[:7].isin(selected_periods)
    ].copy()
    ranked = ranked[
        ranked["publication_link"].fillna("").astype(str).str.strip().ne("")
        & ranked["meta_uploaded_by"].notna()
        & ranked["final_followers"].fillna(0).gt(0)
    ]
    if ranked.empty:
        return ranked

    # A publication may occur in overlapping report uploads. Keep its newest
    # calculated row so it cannot occupy more than one place in the top five.
    ranked = (
        ranked.sort_values(["period_end", "updated_at"], na_position="first")
        .drop_duplicates(["account", "publication_id"], keep="last")
        .sort_values(
            ["final_followers", "post_reach", "publication_date"],
            ascending=[False, False, False],
            na_position="last",
        )
    )
    return ranked.head(limit)


def _render_top_publications(top_publications: pd.DataFrame) -> None:
    st.markdown("### " + tr("Top 5 organic publications", "Топ-5 органических публикаций"))
    st.caption(
        tr(
            "The ranking follows the selected regions and months. Public previews are loaded when Instagram makes them available.",
            "Рейтинг меняется вместе с выбранными регионами и месяцами. Публичные превью загружаются, если Instagram их отдаёт.",
        )
    )
    if top_publications.empty:
        st.info(tr("No linked publications with organic followers in this selection.", "В выбранном периоде нет публикаций с органическими подписчиками и ссылкой."))
        return

    columns = st.columns(len(top_publications))
    for rank, ((_, publication), column) in enumerate(zip(top_publications.iterrows(), columns), start=1):
        with column:
            with st.container(border=True):
                st.markdown(f"#### #{rank} · {int(publication['final_followers']):,}")
                st.caption("Followers organic")
                preview = _load_instagram_preview(str(publication["publication_link"]))
                if preview:
                    preview_bytes, _ = preview
                    st.image(preview_bytes, use_container_width=True)
                else:
                    st.markdown("📷  \n" + tr("Preview unavailable", "Превью недоступно"))
                st.write(f"**{publication['account']}**")
                publication_date = publication.get("publication_date")
                published = str(publication_date)[:10] if pd.notna(publication_date) else ""
                if published:
                    st.caption(published)
                st.caption(
                    f"Total: {int(publication['meta_followers']):,} · "
                    f"Paid: {int(publication['pr_followers']):,} · "
                    f"Reach: {int(publication['post_reach']):,}"
                )
                st.link_button(
                    tr("Open publication", "Открыть публикацию"),
                    str(publication["publication_link"]),
                    use_container_width=True,
                )


def page_dashboard() -> None:
    user = st.session_state.get("user")
    if not has_permission(user, "view_dashboard"):
        st.error(tr("You do not have permission to view the Dashboard.", "У вас нет прав для просмотра Dashboard."))
        return

    hero(
        "Dashboard",
        tr(
            "Organic follower overview across Novakid regions. Totals include complete monthly Meta and paid data, including paid rows without a matched post ID.",
            "Обзор органических подписчиков по регионам Novakid. Итоги включают полные месячные данные Meta и paid, в том числе paid-строки без совпавшего ID поста.",
        ),
        ["Followers organic", "Monthly trend", "Regions", "Organic share"],
    )
    df = db_df("SELECT * FROM monthly_follower_totals ORDER BY period_start DESC, account")
    if df.empty:
        st.info(tr("No monthly follower data yet.", "Пока нет месячных данных по подписчикам."))
        return

    selected_accounts, selected_periods, _ = shared_results_filters(df, show_warnings=False)
    base = df[df["account"].isin(selected_accounts)] if selected_accounts else df.iloc[0:0]
    f = apply_date_filter(base, selected_periods)
    if f.empty:
        st.info(tr("Choose at least one region and period.", "Выберите хотя бы один регион и период."))
        return
    f = f.copy()
    f["month"] = f["month"].astype(str).str[:7]

    organic = int(f["organic_followers"].sum())
    total = int(f["total_followers"].sum())
    paid = int(f["paid_followers"].sum())
    organic_share = organic / total * 100 if total else 0
    regions = int(f["account"].nunique())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Followers organic", f"{organic:,}")
    m2.metric(tr("Organic share", "Доля organic"), f"{organic_share:.1f}%")
    m3.metric(tr("Regions", "Регионов"), f"{regions:,}")
    m4.metric(tr("Selected months", "Выбрано месяцев"), f"{f['month'].nunique():,}")

    organic_tab, context_tab = st.tabs([
        tr("Organic overview", "Обзор organic"),
        tr("Total & paid context", "Контекст total и paid"),
    ])
    with organic_tab:
        c1, c2 = st.columns([1.35, 1])
        monthly_by_account = (
            f.groupby(["month", "account"], as_index=False)["organic_followers"]
            .sum()
            .sort_values(["month", "account"])
        )
        by_region = f.groupby("account", as_index=False)["organic_followers"].sum().sort_values("organic_followers", ascending=False)
        with c1:
            fig = px.bar(
                monthly_by_account,
                x="month",
                y="organic_followers",
                color="account",
                barmode="stack",
                title=tr("Organic by month and region", "Organic по месяцам и регионам"),
                labels={
                    "month": tr("Month", "Месяц"),
                    "organic_followers": "Followers organic",
                    "account": tr("Region", "Регион"),
                },
                color_discrete_sequence=MARK_COLORS,
            )
            _chart_layout(fig, "Followers organic")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            fig = px.bar(by_region, x="account", y="organic_followers", title=tr("Organic by region", "Organic по регионам"))
            _chart_layout(fig, "Followers organic")
            st.plotly_chart(fig, use_container_width=True)

        publications = db_df("SELECT * FROM final_results ORDER BY period_end DESC, updated_at DESC")
        top_publications = _top_organic_publications(publications, selected_accounts, selected_periods)
        _render_top_publications(top_publications)

        st.markdown("### " + tr("Monthly regional overview", "Месячный обзор по регионам"))
        overview = f[["month", "account", "organic_followers", "total_followers"]].copy()
        overview["organic_share"] = overview["organic_followers"].div(overview["total_followers"].replace(0, float("nan"))) * 100
        st.dataframe(overview.sort_values(["month", "organic_followers"], ascending=[False, False]), use_container_width=True, hide_index=True,
                     column_config={"month": tr("Month", "Месяц"), "account": tr("Region", "Регион"),
                                    "organic_followers": "Followers organic", "total_followers": "Followers total",
                                    "organic_share": st.column_config.NumberColumn(tr("Organic share", "Доля organic"), format="%.1f%%")})

    with context_tab:
        c1, c2, c3 = st.columns(3)
        c1.metric("Followers total", f"{total:,}")
        c2.metric("Followers paid", f"{paid:,}")
        c3.metric("Followers organic", f"{organic:,}")
        mix = f.groupby("month", as_index=False)[["organic_followers", "paid_followers"]].sum().sort_values("month")
        fig = px.bar(mix, x="month", y=["organic_followers", "paid_followers"], barmode="stack",
                     title=tr("Follower mix by month", "Структура подписчиков по месяцам"),
                     labels={"value": "Followers", "variable": tr("Type", "Тип")})
        _chart_layout(fig, "Followers")
        st.plotly_chart(fig, use_container_width=True)
