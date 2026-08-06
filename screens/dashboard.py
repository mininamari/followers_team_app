from __future__ import annotations

import plotly.express as px
import streamlit as st

from core.auth import has_permission
from core.db import db_df
from core.i18n import tr
from core.style import hero
from screens._shared import apply_date_filter, shared_results_filters


def _chart_layout(fig, y_title: str) -> None:
    fig.update_layout(
        template="plotly_white", title_font_size=20, legend_title_text=tr("Region", "Регион"),
        xaxis_title="", yaxis_title=y_title, bargap=0.25,
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
                color_discrete_sequence=px.colors.qualitative.Bold,
            )
            _chart_layout(fig, "Followers organic")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            fig = px.bar(by_region, x="account", y="organic_followers", title=tr("Organic by region", "Organic по регионам"))
            _chart_layout(fig, "Followers organic")
            st.plotly_chart(fig, use_container_width=True)

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
