from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil

import streamlit as st

from core.auth import LoginRateLimited, authenticate, get_user, has_permission
from core.config import ROLE_LABELS, SESSION_IDLE_TIMEOUT_MINUTES, SESSION_MAX_AGE_HOURS, now_utc, parse_utc
from core.db import db_df, init_db
from core.i18n import LANGUAGE_LABELS, LANGUAGE_OPTIONS, current_language, set_language_from_label, tr
from core.style import apply_novakid_style, hero
from screens.backups import page_backups
from screens.dashboard import page_dashboard
from screens.facebook_ads import page_facebook_ads
from screens.history import page_upload_history
from screens.profile import page_profile
from screens.reports import page_report
from screens.upload_meta import page_upload_meta
from screens.upload_pr import page_upload_pr
from screens.users import page_users


def login_screen() -> None:
    st.set_page_config(page_title="Novakid Social Reports", layout="wide", page_icon="⭐")
    apply_novakid_style()
    left, mid, right = st.columns([1, 1.25, 1])
    with mid:
        auth_notice = st.session_state.pop("auth_notice", None)
        if auth_notice:
            st.warning(auth_notice)
        language_label = LANGUAGE_LABELS.get(current_language(), "English")
        st.selectbox(
            tr("Language", "Язык"),
            list(LANGUAGE_OPTIONS.keys()),
            index=list(LANGUAGE_OPTIONS.keys()).index(language_label),
            on_change=lambda: set_language_from_label(st.session_state["login_language"]),
            key="login_language",
        )
        hero(
            "Novakid Social Reports",
            tr(
                "Sign in to upload Meta and PR CSV files and monitor total, paid, and organic followers by region.",
                "Войдите, чтобы загружать Meta и PR CSV и следить за total, paid и organic подписчиками по регионам.",
            ),
            ["Followers total", "Followers paid", "Followers organic", "Team access"],
        )
        with st.form("login"):
            st.markdown("### " + tr("Team Login", "Вход в командный кабинет"))
            username = st.text_input(tr("Username", "Логин"))
            password = st.text_input(tr("Password", "Пароль"), type="password")
            submitted = st.form_submit_button(tr("Sign in", "Войти"), type="primary", use_container_width=True)
        if submitted:
            try:
                user = authenticate(username, password)
                if user:
                    signed_in_at = now_utc()
                    st.session_state["user"] = {
                        "username": user["username"],
                        "role": user["role"],
                        "auth_version": int(user.get("auth_version", 1)),
                    }
                    st.session_state["auth_started_at"] = signed_in_at
                    st.session_state["auth_last_activity_at"] = signed_in_at
                    st.rerun()
                else:
                    st.error(tr("Incorrect username or password.", "Неверный логин или пароль."))
            except LoginRateLimited as exc:
                minutes = max(1, ceil(exc.retry_after_seconds / 60))
                st.error(tr(
                    f"Too many login attempts. Try again in {minutes} min.",
                    f"Слишком много попыток входа. Попробуйте снова через {minutes} мин.",
                ))
        if db_df("SELECT COUNT(*) AS user_count FROM users")["user_count"].iloc[0] == 0:
            st.warning(tr(
                "No users have been created yet. The first admin is configured through environment variables.",
                "Пользователи еще не созданы. Администратор первого запуска задается через переменные окружения.",
            ))


def _end_session(message: str) -> None:
    for key in ("user", "auth_started_at", "auth_last_activity_at"):
        st.session_state.pop(key, None)
    st.session_state["auth_notice"] = message
    st.rerun()


def require_login() -> dict:
    if "user" not in st.session_state:
        login_screen()
        st.stop()

    session_user = st.session_state["user"]
    started_at = parse_utc(st.session_state.get("auth_started_at"))
    last_activity_at = parse_utc(st.session_state.get("auth_last_activity_at"))
    now = datetime.utcnow()
    expired_message = tr("Your session has expired. Sign in again.", "Сессия завершена. Войдите снова.")
    if (
        not started_at
        or not last_activity_at
        or now - started_at >= timedelta(hours=SESSION_MAX_AGE_HOURS)
        or now - last_activity_at >= timedelta(minutes=SESSION_IDLE_TIMEOUT_MINUTES)
    ):
        _end_session(expired_message)

    current_user = get_user(session_user.get("username", ""))
    if not current_user or not current_user["is_active"]:
        _end_session(tr("Your access has been disabled. Contact an administrator.", "Ваш доступ отключён. Обратитесь к администратору."))
    if int(current_user.get("auth_version", 1)) != int(session_user.get("auth_version", 0)):
        _end_session(tr("Your account security settings changed. Sign in again.", "Настройки безопасности аккаунта изменились. Войдите снова."))

    session_user["role"] = current_user["role"]
    st.session_state["auth_last_activity_at"] = now_utc()
    return session_user


def sidebar(user: dict) -> str:
    with st.sidebar:
        st.markdown("# ⭐ Novakid")
        st.caption("Social Reports")
        language_label = LANGUAGE_LABELS.get(current_language(), "English")
        st.selectbox(
            tr("Language", "Язык"),
            list(LANGUAGE_OPTIONS.keys()),
            index=list(LANGUAGE_OPTIONS.keys()).index(language_label),
            on_change=lambda: set_language_from_label(st.session_state["sidebar_language"]),
            key="sidebar_language",
        )
        st.divider()
        st.write(f"**{user['username']}**")
        st.caption(f"{tr('role', 'роль')}: {ROLE_LABELS.get(user['role'], user['role'])}")
        pages = []
        if has_permission(user, "view_dashboard"):
            pages.append(("Dashboard", "Dashboard"))
        if has_permission(user, "upload_meta"):
            pages.append(("Upload Meta", tr("Upload Meta", "Загрузка Meta")))
        if has_permission(user, "upload_pr"):
            pages.append(("Upload PR", tr("Upload PR", "Загрузка PR")))
        if has_permission(user, "view_reports"):
            pages.append(("Reports", tr("Reports", "Отчеты")))
        if has_permission(user, "view_fb_ads"):
            pages.append(("Facebook Ads", "Facebook Ads"))
        if has_permission(user, "view_history"):
            pages.append(("Upload history", tr("Upload history", "История загрузок")))
        if has_permission(user, "manage_users"):
            pages.append(("Users", tr("Users", "Пользователи")))
        if has_permission(user, "manage_backups"):
            pages.append(("Backups", tr("Backups", "Резервные копии")))
        pages.append(("Profile", tr("Profile", "Профиль")))
        page_ids = [page_id for page_id, _ in pages]
        page_labels = dict(pages)
        page = st.radio(
            tr("Navigation", "Навигация"),
            page_ids,
            format_func=lambda page_id: page_labels[page_id],
            label_visibility="collapsed",
        )
        st.divider()
        if st.button(tr("Log out", "Выйти"), use_container_width=True):
            for key in ("user", "auth_started_at", "auth_last_activity_at"):
                st.session_state.pop(key, None)
            st.rerun()
    return page


def main() -> None:
    init_db()
    user = require_login()
    st.set_page_config(page_title="Novakid Social Reports", layout="wide", page_icon="⭐")
    apply_novakid_style()
    page = sidebar(user)
    if page == "Dashboard":
        page_dashboard()
    elif page == "Upload Meta":
        page_upload_meta(user)
    elif page == "Upload PR":
        page_upload_pr(user)
    elif page == "Reports":
        page_report(user)
    elif page == "Facebook Ads":
        page_facebook_ads(user)
    elif page == "Upload history":
        page_upload_history()
    elif page == "Users":
        page_users(user)
    elif page == "Backups":
        page_backups(user)
    elif page == "Profile":
        page_profile(user)
    else:
        page_dashboard()


if __name__ == "__main__":
    main()
