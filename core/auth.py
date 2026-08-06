from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Optional

from core.config import (
    LOGIN_ATTEMPT_WINDOW_MINUTES,
    LOGIN_LOCKOUT_MINUTES,
    LOGIN_MAX_ATTEMPTS,
    PERMISSIONS,
    now_utc,
    parse_utc,
)
from core.database import connect_db
from core.i18n import tr


class LoginRateLimited(Exception):
    def __init__(self, retry_after_seconds: int):
        super().__init__("Too many login attempts")
        self.retry_after_seconds = max(1, retry_after_seconds)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return "pbkdf2_sha256$200000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(got, expected)
    except Exception:
        return False


def has_permission(user: Optional[dict], permission: str) -> bool:
    if not user:
        return False
    return permission in PERMISSIONS.get(user.get("role", ""), set())


def require_permission(user: Optional[dict], permission: str) -> None:
    if not has_permission(user, permission):
        raise PermissionError(tr("You do not have permission for this action.", "У вас нет прав для этого действия."))


def get_user(username: str) -> Optional[dict]:
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None


def _login_lock_remaining(username: str) -> int:
    with connect_db() as conn:
        row = conn.execute(
            "SELECT locked_until FROM login_attempts WHERE username=?",
            (username,),
        ).fetchone()
    if not row or not row[0]:
        return 0
    locked_until = parse_utc(row[0])
    if not locked_until:
        return 0
    return max(0, int((locked_until - datetime.utcnow()).total_seconds()))


def _record_failed_login(username: str) -> int:
    now = datetime.utcnow()
    with connect_db() as conn:
        row = conn.execute(
            "SELECT failed_count, window_started_at FROM login_attempts WHERE username=?",
            (username,),
        ).fetchone()
        window_started = parse_utc(row[1]) if row else None
        if not window_started or now - window_started >= timedelta(minutes=LOGIN_ATTEMPT_WINDOW_MINUTES):
            failed_count = 1
            window_started_at = now_utc()
        else:
            failed_count = int(row[0]) + 1
            window_started_at = row[1]

        locked_until = None
        if failed_count >= LOGIN_MAX_ATTEMPTS:
            locked_until = (now + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)).isoformat(timespec="seconds") + "Z"

        conn.execute(
            """
            INSERT INTO login_attempts(username, failed_count, window_started_at, locked_until)
            VALUES(?,?,?,?)
            ON CONFLICT(username) DO UPDATE SET
                failed_count=excluded.failed_count,
                window_started_at=excluded.window_started_at,
                locked_until=excluded.locked_until
            """,
            (username, failed_count, window_started_at, locked_until),
        )
        conn.commit()
    return LOGIN_LOCKOUT_MINUTES * 60 if locked_until else 0


def clear_login_attempts(username: str) -> None:
    with connect_db() as conn:
        conn.execute("DELETE FROM login_attempts WHERE username=?", (username,))
        conn.commit()


def authenticate(username: str, password: str) -> Optional[dict]:
    username = username.strip()
    remaining = _login_lock_remaining(username)
    if remaining:
        raise LoginRateLimited(remaining)

    user = get_user(username)
    if user and user["is_active"] and verify_password(password, user["password_hash"]):
        clear_login_attempts(username)
        return user
    # Persist counters only for real accounts so arbitrary usernames cannot
    # grow the login_attempts table without bound.
    if user:
        remaining = _record_failed_login(username)
        if remaining:
            raise LoginRateLimited(remaining)
    return None
