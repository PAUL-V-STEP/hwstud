from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from starlette import status

from .db import transaction, utc_now_iso
from .services.registration import group_registration_error

NICK_RE = re.compile(r"^[A-Za-z0-9_-]{3,24}$")
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1


@dataclass(frozen=True, slots=True)
class Actor:
    role: str
    student_id: int | None = None
    nick: str | None = None
    group_id: int | None = None
    group_name: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class RateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events.setdefault(key, [])
            cutoff = now - window_seconds
            events[:] = [item for item in events if item > cutoff]
            return len(events) < limit

    def register(self, key: str, window_seconds: int) -> None:
        now = time.monotonic()
        with self._lock:
            events = self._events.setdefault(key, [])
            cutoff = now - window_seconds
            events[:] = [item for item in events if item > cutoff]
            events.append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


def normalize_nick(nick: str) -> str:
    return nick.strip().lower()


def validate_nick(nick: str) -> str | None:
    if not NICK_RE.fullmatch(nick):
        return "Ник: 3–24 символа, только латинские буквы, цифры, _ и -."
    return None


def find_student(conn: sqlite3.Connection, nick: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT s.*, g.name AS group_name "
        "FROM students s JOIN groups g ON g.id = s.group_id "
        "WHERE s.nick_norm = ?",
        (normalize_nick(nick),),
    ).fetchone()


def create_student(
    conn: sqlite3.Connection, nick: str, group_id: int, *, require_window: bool = False
) -> tuple[sqlite3.Row | None, str | None]:
    nick = nick.strip()
    error = validate_nick(nick)
    if error:
        return None, error
    try:
        with transaction(conn, immediate=True):
            group = conn.execute(
                "SELECT * FROM groups WHERE id = ?", (group_id,)
            ).fetchone()
            if not group:
                return None, "Выберите группу."
            if require_window:
                window_error = group_registration_error(group)
                if window_error:
                    return None, window_error
            count = conn.execute(
                "SELECT COUNT(*) FROM students WHERE group_id = ?", (group_id,)
            ).fetchone()[0]
            if count >= group["max_members"]:
                return None, "В группе уже 20 участников."
            if find_student(conn, nick):
                return None, "Такой ник уже занят."
            cursor = conn.execute(
                "INSERT INTO students(group_id, nick, nick_norm, created_at, login_token) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    group_id,
                    nick,
                    normalize_nick(nick),
                    utc_now_iso(),
                    secrets.token_hex(16),
                ),
            )
            student_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        return None, "Такой ник уже занят."
    row = conn.execute(
        "SELECT s.*, g.name AS group_name "
        "FROM students s JOIN groups g ON g.id = s.group_id WHERE s.id = ?",
        (student_id,),
    ).fetchone()
    return row, None


def login_student(request: Request, student: sqlite3.Row) -> None:
    request.session.clear()
    request.session.update(
        {
            "role": "student",
            "student_id": student["id"],
            "login_token": student["login_token"],
            "csrf": secrets.token_urlsafe(32),
        }
    )


def login_admin(request: Request, must_change_password: bool = False) -> None:
    request.session.clear()
    session_data = {"role": "admin", "csrf": secrets.token_urlsafe(32)}
    if must_change_password:
        session_data["must_change_password"] = True
    request.session.update(session_data)


def current_actor(request: Request) -> Actor | None:
    session = request.session
    role = session.get("role")
    if role == "admin":
        return Actor(role="admin")
    if role != "student":
        return None
    student_id = session.get("student_id")
    token = session.get("login_token")
    if not isinstance(student_id, int) or not token:
        return None
    conn = request.app.state.db_connect()
    try:
        row = conn.execute(
            "SELECT s.*, g.name AS group_name FROM students s "
            "JOIN groups g ON g.id = s.group_id WHERE s.id = ?",
            (student_id,),
        ).fetchone()
    finally:
        conn.close()
    if (
        not row
        or row["blocked"]
        or not hmac.compare_digest(str(row["login_token"]), str(token))
    ):
        session.clear()
        return None
    return Actor(
        role="student",
        student_id=row["id"],
        nick=row["nick"],
        group_id=row["group_id"],
        group_name=row["group_name"],
    )


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return str(token)


def check_csrf(request: Request, supplied: str) -> None:
    expected = request.session.get("csrf")
    if not expected or not supplied or not hmac.compare_digest(str(expected), supplied):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Сессия формы устарела. Обновите страницу и повторите.",
        )


def hash_password(
    password: str,
    *,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
) -> str:
    if not password:
        raise ValueError("Пароль не должен быть пустым")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=64,
        maxmem=64 * 1024 * 1024,
    )
    encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
    return f"scrypt${n}${r}${p}${encode(salt)}${encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, digest_text = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        salt = decode(salt_text)
        expected = decode(digest_text)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def verify_admin_password(settings, conn, password: str) -> tuple[bool, bool]:
    """Возвращает (верен ли пароль, использован ли начальный admin)."""
    stored = conn.execute(
        "SELECT value FROM settings WHERE key = 'admin_password_hash'"
    ).fetchone()
    if stored and stored["value"]:
        return verify_password(password, stored["value"]), False
    if settings.admin_password_hash:
        return verify_password(password, settings.admin_password_hash), False
    changed = conn.execute(
        "SELECT value FROM settings WHERE key = 'admin_password_changed'"
    ).fetchone()
    is_default = not changed or changed["value"] != "1"
    valid = is_default and hmac.compare_digest(password, "admin")
    return valid, bool(valid)


def change_admin_password(
    request: Request, conn, current_password: str, new_password: str, confirmation: str
) -> None:
    valid, _default = verify_admin_password(
        request.app.state.settings, conn, current_password
    )
    if not valid:
        raise ValueError("Текущий пароль неверен")
    if len(new_password) < 12:
        raise ValueError("Новый пароль должен содержать не менее 12 символов")
    if new_password.lower() == "admin":
        raise ValueError("Начальный пароль admin нельзя использовать постоянно")
    if new_password == current_password:
        raise ValueError("Новый пароль должен отличаться от текущего")
    if new_password != confirmation:
        raise ValueError("Повтор нового пароля не совпадает")
    encoded = hash_password(new_password)
    with transaction(conn, immediate=True):
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('admin_password_hash', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (encoded,),
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('admin_password_changed', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, object) "
            "VALUES (?, 'admin', 'password_changed', NULL)",
            (utc_now_iso(),),
        )
    request.session.pop("must_change_password", None)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def flash(request: Request, category: str, text: str) -> None:
    flashes = list(request.session.get("flashes", []))
    flashes.append({"category": category, "text": text})
    request.session["flashes"] = flashes[-3:]


def pop_flashes(request: Request) -> list[dict[str, Any]]:
    return list(request.session.pop("flashes", []))
