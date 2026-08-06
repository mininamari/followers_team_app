from __future__ import annotations

import streamlit as st

from core.auth import has_permission
from core.config import PR_FOLLOWERS_COL, PR_PAGE_COL
from core.csv_import import import_pr, pr_page_summary, read_table_any
from core.db import accounts_in_db
from core.i18n import tr
from core.style import hero


def page_upload_pr(user: dict) -> None:
    if not has_permission(user, "upload_pr"):
        st.error(tr("You do not have permission to upload PR CSV files.", "У вас нет прав для загрузки PR CSV."))
        return
    hero(
        "Upload PR",
        tr(
            "Upload a CSV from the ad account. The app can distribute a shared PR file by matching publication IDs from Meta.",
            "Таргетолог загружает CSV из рекламного кабинета. Система распределит общий PR-файл по аккаунтам через ID публикации из Meta.",
        ),
        ["Auto account distribution", "Spend", "CPF", "Auto recalc"],
    )
    auto_detect = st.checkbox(tr("Automatically distribute by Meta publication ID", "Автоматически распределить по аккаунтам через ID публикации из Meta"), value=True)
    existing = accounts_in_db()
    default_options = existing + ["novakid_israel", "novakid_france", "novakid_spain", "novakid_turkey"]
    default_options = sorted(set(default_options))
    account = ""
    if not auto_detect:
        selected = st.selectbox(tr("Region / account", "Регион / аккаунт"), default_options, index=0 if default_options else None)
        custom = st.text_input(tr("Or enter a new account manually", "Или введите новый аккаунт вручную"), placeholder="novakid_germany")
        account = custom.strip() or selected
    add_mode_label = tr("Add only new rows", "Добавить только новые строки")
    update_mode_label = tr("Update existing + add new", "Обновить существующие + добавить новые")
    import_mode = st.radio(
        tr("Import mode", "Режим импорта"),
        [add_mode_label, update_mode_label],
        horizontal=True,
        help=tr(
            "Manual corrections are preserved in both modes.",
            "Ручные корректировки сохраняются в обоих режимах.",
        ),
    )
    add_only = import_mode == add_mode_label
    if add_only:
        st.caption(tr(
            "Safe mode: existing imported rows and manual corrections remain unchanged.",
            "Безопасный режим: существующие импортированные строки и ручные корректировки не изменятся.",
        ))
    else:
        st.caption(tr(
            "Imported rows for the mapped accounts and period will be replaced by this file. Manual corrections remain in effect.",
            "Импортированные строки выбранных аккаунтов и периода будут заменены данными этого файла. Ручные корректировки продолжат действовать.",
        ))
    pr_file = st.file_uploader(tr("Novakid PR CSV / Excel", "CSV / Excel из Novakid PR"), type=["csv", "xlsx"], key="pr")
    page_mapping: dict[str, str] = {}
    if pr_file:
        try:
            preview = read_table_any(pr_file)
            st.dataframe(preview.head(10), use_container_width=True, hide_index=True)
            pages = pr_page_summary(pr_file)
            if not pages.empty:
                st.markdown("#### " + tr("Page mapping", "Сопоставление страниц"))
                options = sorted(set(existing + pages["account"].dropna().astype(str).tolist()) - {""})
                mapped_pages = st.data_editor(
                    pages,
                    use_container_width=True,
                    hide_index=True,
                    disabled=[PR_PAGE_COL, PR_FOLLOWERS_COL],
                    column_config={
                        PR_PAGE_COL: tr("Facebook page", "Страница Facebook"),
                        PR_FOLLOWERS_COL: tr("Paid followers", "Платные подписчики"),
                        "account": st.column_config.SelectboxColumn(
                            tr("Instagram account", "Instagram-аккаунт"), options=options, required=True,
                        ),
                    },
                    key="pr_page_mapping",
                )
                page_mapping = dict(zip(mapped_pages[PR_PAGE_COL], mapped_pages["account"]))
                st.caption(tr(
                    f"File total: {int(pages[PR_FOLLOWERS_COL].sum()):,} paid followers.",
                    f"Итого в файле: {int(pages[PR_FOLLOWERS_COL].sum()):,} платных подписчиков.",
                ))
        except Exception as exc:
            st.error(str(exc))
    if st.button(tr("Save PR and recalculate", "Сохранить PR и пересчитать"), type="primary", use_container_width=True):
        if not pr_file:
            st.error(tr("Upload a CSV file.", "Загрузите CSV."))
        else:
            try:
                rows, warnings = import_pr(
                    pr_file, user, account, auto_detect,
                    page_account_map=page_mapping,
                    add_only=add_only,
                )
                if page_mapping:
                    target = tr("by Page Name", "по названиям страниц")
                elif auto_detect:
                    target = tr("by Meta accounts", "по аккаунтам из Meta")
                else:
                    target = tr(f"for {account}", f"для {account}")
                st.success(tr(f"PR saved {target}. Rows: {rows}. The report was recalculated automatically.", f"PR сохранен {target}. Строк: {rows}. Отчет пересчитан автоматически."))
                for w in warnings:
                    st.warning(w)
            except Exception as exc:
                st.error(str(exc))
