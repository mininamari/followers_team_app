from __future__ import annotations

import streamlit as st

from core.auth import has_permission
from core.csv_import import import_meta, infer_accounts_from_meta, read_csv_any
from core.db import db_df
from core.i18n import tr
from core.style import hero
from integrations.ads_paid_followers import refresh_paid_from_ads_api
from integrations.facebook_ads_client import is_configured


def _show_messages(warnings: list[str]) -> None:
    for message in warnings:
        st.warning(message)


def _render_paid_refresh(user: dict) -> None:
    periods = db_df(
        "SELECT DISTINCT period_start, period_end FROM meta_publications ORDER BY period_start DESC, period_end DESC"
    )
    if periods.empty:
        return
    with st.expander(tr("Reload paid for an uploaded period", "Обновить paid за загруженный период")):
        st.caption(tr(
            "Loads paid followers and spend from the ad account again, for example after late attributed follows. Manual corrections stay in effect.",
            "Заново загружает paid-подписчиков и расходы из рекламного кабинета, например после позднего начисления подписок. Ручные корректировки продолжают действовать.",
        ))
        options = [f"{row.period_start} – {row.period_end}" for row in periods.itertuples()]
        selected = st.selectbox(tr("Meta period", "Период Meta"), options, key="paid_refresh_period")
        if st.button(tr("Reload paid from the ad account", "Обновить paid из рекламного кабинета"), use_container_width=True):
            period_start, period_end = selected.split(" – ")
            with st.spinner(tr("Syncing the ad account...", "Синхронизация рекламного кабинета...")):
                warnings = refresh_paid_from_ads_api(period_start, period_end, user)
            _show_messages(warnings)


def page_upload_meta(user: dict) -> None:
    if not has_permission(user, "upload_meta"):
        st.error(tr("You do not have permission to upload Meta CSV files.", "У вас нет прав для загрузки Meta CSV."))
        return
    hero(
        "Upload Meta",
        tr(
            "Upload a CSV from Meta Business Suite. Paid followers and spend for the same period are loaded from the ad account automatically.",
            "Загрузите CSV из Meta Business Suite. Paid-подписчики и расходы за тот же период подтягиваются из рекламного кабинета автоматически.",
        ),
        ["RU/EN columns", "Combined exports", "Paid from Ads API", "Post links"],
    )
    if not is_configured():
        st.warning(tr(
            "META_ACCESS_TOKEN is not configured: Meta data will be saved without paid followers from the ad account.",
            "META_ACCESS_TOKEN не настроен: данные Meta сохранятся без paid-подписчиков из рекламного кабинета.",
        ))
    meta_file = st.file_uploader(tr("Meta Business Suite CSV", "CSV из Meta Business Suite"), type=["csv"], key="meta")
    c1, c2 = st.columns(2)
    manual_start = c1.date_input(tr("Period start if it cannot be detected from the filename", "Начало периода, если не определяется из имени файла"), value=None)
    manual_end = c2.date_input(tr("Period end if it cannot be detected from the filename", "Конец периода, если не определяется из имени файла"), value=None)
    if meta_file:
        try:
            preview = read_csv_any(meta_file)
            accs = infer_accounts_from_meta(preview)
            st.success(tr("Detected accounts: ", "Найденные аккаунты: ") + (", ".join(accs) if accs else tr("could not detect", "не удалось определить")))
            st.dataframe(preview.head(10), use_container_width=True, hide_index=True)
        except Exception as exc:
            st.error(str(exc))
    if st.button(tr("Save Meta, load paid and recalculate", "Сохранить Meta, загрузить paid и пересчитать"), type="primary", use_container_width=True):
        if not meta_file:
            st.error(tr("Upload a CSV file.", "Загрузите CSV."))
        else:
            try:
                with st.spinner(tr("Saving Meta and syncing the ad account...", "Сохраняем Meta и синхронизируем рекламный кабинет...")):
                    rows, warnings = import_meta(meta_file, user, manual_start, manual_end, paid_from_ads_api=True)
                st.success(tr(f"Meta saved. Rows: {rows}. The report was recalculated automatically.", f"Meta сохранена. Строк: {rows}. Отчет пересчитан автоматически."))
                _show_messages(warnings)
            except Exception as exc:
                st.error(str(exc))
    _render_paid_refresh(user)
