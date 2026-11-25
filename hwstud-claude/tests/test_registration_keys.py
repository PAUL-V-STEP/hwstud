from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.auth import RateLimiter, client_ip, create_student
from app.db import connect, migrate
from app.services import backup, personal_keys, registration
from app.services.access_cards import AccessCardStore
from app.services.rate_limits import (
    check_login_attempt,
    check_registration,
    record_registration,
    reset_login_nick,
)

from .conftest import csrf_from, login_student, open_registration


def test_key_dictionary_is_strict(settings):
    words = personal_keys.load_key_words()
    assert len(words) == 2048
    assert len(set(words)) == 2048
    assert all(re.fullmatch(r"[а-ик-щыьэюя]{3,8}", word) for word in words)
    for index, first in enumerate(words):
        for second in words[index + 1:]:
            assert not _distance_one(first, second), (first, second)


def _distance_one(first: str, second: str) -> bool:
    if abs(len(first) - len(second)) > 1:
        return False
    if len(first) == len(second):
        return sum(a != b for a, b in zip(first, second)) == 1
    if len(first) > len(second):
        first, second = second, first
    return any(
        second[:index] + second[index + 1:] == first
        for index in range(len(second))
    )


def test_key_generation_and_normalization(monkeypatch):
    monkeypatch.setattr(
        personal_keys.secrets, "choice", lambda words: words[0]
    )
    first = personal_keys.load_key_words()[0]
    assert personal_keys.generate_key() == f"{first}-{first}-{first}"
    monkeypatch.undo()
    expected = personal_keys.normalize_key("липа-ракета-гром")
    for value in (
        "Липа Ракета Гром", "липа ракета гром", "ЛИПА-РАКЕТА-ГРОМ",
        "kbgf hfrtnf uhjv", "липа  ракета-гром", "липа+ракета_гром",
    ):
        assert personal_keys.normalize_key(value) == expected
    for value in ("липа ракета", "липа ракета гром лес", "липа неизвестное слово"):
        with pytest.raises(ValueError, match="Ник или ключ не подходят"):
            personal_keys.normalize_key(value)


def test_corrupted_dictionary_prevents_startup(tmp_path):
    path = tmp_path / "broken.txt"
    path.write_text("липа\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="2048"):
        personal_keys.load_key_words(path)


def test_registration_window_expires_without_background_job(settings, monkeypatch):
    migrate(settings)
    current = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(registration, "utc_now", lambda: current)
    conn = connect(settings)
    try:
        assert registration.window_minutes(conn) == 10
        registration.open_window(conn, [1], now=current)
        assert registration.group_is_open(conn, 1, now=current)
        assert not registration.group_is_open(conn, 2, now=current)
        student, error = create_student(conn, "window_student", 1, require_window=True)
        assert not error and student
        current = current + timedelta(minutes=10, seconds=1)
        assert not registration.group_is_open(conn, 1, now=current)
        student, error = create_student(conn, "late_student", 1, require_window=True)
        assert not student
        assert error.startswith("Время регистрации истекло")
    finally:
        conn.close()


def test_window_duration_must_be_between_5_and_15(settings):
    migrate(settings)
    conn = connect(settings)
    try:
        for value in ("4", "16"):
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'registration_window_minutes'",
                (value,),
            )
            with pytest.raises(ValueError, match="от 5 до 15"):
                registration.open_window(conn, [1])
    finally:
        conn.close()


def test_registration_extension_cap_and_close(settings):
    migrate(settings)
    current = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    conn = connect(settings)
    try:
        registration.open_window(conn, [1, 2], now=current)
        for _ in range(5):
            registration.extend_window(conn, 1, now=current)
        with pytest.raises(ValueError, match="60 минут"):
            registration.extend_window(conn, 1, now=current)
        assert registration.close_windows(conn, 1) == [1]
        assert not registration.group_is_open(conn, 1, now=current)
        assert registration.group_is_open(conn, 2, now=current)
        assert registration.close_windows(conn, close_all=True) == [2]
        assert registration.status(conn, now=current)["windows"] == []
        actions = {
            row[0] for row in conn.execute(
                "SELECT action FROM audit_log WHERE action LIKE 'registration_%'"
            )
        }
        assert {"registration_opened", "registration_extended", "registration_closed"} <= actions
    finally:
        conn.close()


def test_status_contains_only_students_from_current_window(settings):
    migrate(settings)
    current = datetime.now(UTC).replace(microsecond=0)
    conn = connect(settings)
    try:
        create_student(conn, "before_window", 1)
        conn.execute(
            "UPDATE students SET created_at = '2000-01-01T00:00:00Z' "
            "WHERE nick_norm = 'before_window'"
        )
        registration.open_window(conn, [1], now=current)
        create_student(conn, "during_window", 1)
        result = registration.status(conn, now=current)
        assert result["windows"][0]["seconds_left"] == 600
        assert [item["nick"] for item in result["new_students"]] == ["during_window"]
    finally:
        conn.close()


def test_rate_limits_by_nick_ip_and_registration():
    limiter = RateLimiter()
    for _ in range(10):
        error, pair_key = check_login_attempt(limiter, "192.0.2.1", "one_nick")
        assert not error
    error, pair_key = check_login_attempt(limiter, "192.0.2.1", "one_nick")
    assert "для этого ника" in error
    error, _ = check_login_attempt(limiter, "192.0.2.1", "other_nick")
    assert not error
    reset_login_nick(limiter, pair_key)
    error, _ = check_login_attempt(limiter, "192.0.2.1", "one_nick")
    assert not error

    limiter = RateLimiter()
    for index in range(300):
        error, _ = check_login_attempt(limiter, "198.51.100.1", f"nick_{index}")
        assert not error
    error, _ = check_login_attempt(limiter, "198.51.100.1", "overflow")
    assert "с вашей сети" in error

    limiter = RateLimiter()
    for _ in range(60):
        assert check_registration(limiter, "203.0.113.1") is None
        record_registration(limiter, "203.0.113.1")
    assert "много регистраций" in check_registration(limiter, "203.0.113.1")


def test_classroom_of_25_students_is_not_blocked(settings):
    migrate(settings)
    conn = connect(settings)
    try:
        registration.open_window(conn, [1, 2])
        limiter = RateLimiter()
        for index in range(25):
            nick = f"classroom_{index}"
            group_id = 1 if index < 20 else 2
            error, _ = check_login_attempt(limiter, "192.0.2.10", nick)
            assert not error
            assert check_registration(limiter, "192.0.2.10") is None
            student, create_error = create_student(conn, nick, group_id, require_window=True)
            assert student and not create_error
            record_registration(limiter, "192.0.2.10")
        student, error = create_student(conn, "classroom_25", 1, require_window=True)
        assert not student and error == "В группе уже 20 участников."
    finally:
        conn.close()


def _assert_not_in_database(conn, plaintext: str) -> None:
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    for table_row in tables:
        table = table_row[0].replace('"', '""')
        columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        for column in columns:
            name = column[1].replace('"', '""')
            found = conn.execute(
                f'SELECT 1 FROM "{table}" WHERE instr(CAST("{name}" AS TEXT), ?) > 0 LIMIT 1',
                (plaintext,),
            ).fetchone()
            assert not found, f"Открытый ключ найден в {table_row[0]}.{column[1]}"


def test_public_registration_rejects_closed_and_expired_window(client):
    response = client.get("/login")
    response = client.post(
        "/login", data={"nick": "closed_student", "csrf": csrf_from(response)}
    )
    assert response.status_code == 200
    assert "Регистрация сейчас закрыта" in response.text

    open_registration(client)
    response = client.get("/login")
    response = client.post(
        "/login", data={"nick": "expired_student", "csrf": csrf_from(response)}
    )
    assert response.status_code == 200
    conn = client.app.state.db_connect()
    try:
        conn.execute(
            "UPDATE groups SET enroll_until = '2000-01-01T00:00:00Z' WHERE id = 1"
        )
    finally:
        conn.close()
    response = client.post(
        "/login",
        data={
            "nick": "expired_student", "group_id": 1, "csrf": csrf_from(response)
        },
    )
    assert response.status_code == 400
    assert "Время регистрации истекло" in response.text
    conn = client.app.state.db_connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0
    finally:
        conn.close()


def test_key_hash_lock_and_reset(settings):
    migrate(settings)
    conn = connect(settings)
    try:
        student, error = create_student(conn, "key_student", 1)
        assert not error
        old_login_token = student["login_token"]
        key = personal_keys.issue_key(conn, student["id"], "admin")
        student = conn.execute(
            "SELECT * FROM students WHERE id = ?", (student["id"],)
        ).fetchone()
        assert student["key_hash"] != key
        assert student["key_issued_by"] == "admin"
        assert student["login_token"] != old_login_token
        assert personal_keys.verify_key(key, student["key_hash"])
        assert not personal_keys.verify_key("неправильный", student["key_hash"])
        current = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
        for expected in ("invalid", "invalid", "invalid", "invalid", "locked"):
            assert personal_keys.check_student_key(
                conn, student, "неправильный", now=current
            ) == expected
            student = conn.execute(
                "SELECT * FROM students WHERE id = ?", (student["id"],)
            ).fetchone()
        assert student["key_failed_count"] == 5
        assert student["key_locked_until"]
        conn.close()
        conn = connect(settings)
        student = conn.execute(
            "SELECT * FROM students WHERE id = ?", (student["id"],)
        ).fetchone()
        assert personal_keys.check_student_key(
            conn, student, key, now=current + timedelta(minutes=1)
        ) == "locked"
        current = current + timedelta(minutes=16)
        assert personal_keys.check_student_key(
            conn, student, "неправильный", now=current
        ) == "invalid"
        new_key = personal_keys.issue_key(conn, student["id"], "admin")
        student = conn.execute(
            "SELECT * FROM students WHERE id = ?", (student["id"],)
        ).fetchone()
        assert student["key_failed_count"] == 0
        assert student["key_locked_until"] is None
        assert personal_keys.verify_key(new_key, student["key_hash"])
        assert personal_keys.check_student_key(
            conn, student, new_key, now=current + timedelta(minutes=20)
        ) == "ok"
        student = conn.execute(
            "SELECT * FROM students WHERE id = ?", (student["id"],)
        ).fetchone()
        assert student["last_login_at"]
        assert student["key_failed_count"] == 0
        for plaintext in (key, new_key):
            assert plaintext not in str(dict(student))
            assert plaintext not in str(
                conn.execute("SELECT * FROM audit_log").fetchall()
            )
            _assert_not_in_database(conn, plaintext)
    finally:
        conn.close()


def test_key_fields_survive_database_backup_without_plaintext(settings):
    migrate(settings)
    conn = connect(settings)
    try:
        student, error = create_student(conn, "backup_key_student", 1)
        assert not error
        key = personal_keys.issue_key(conn, student["id"], "transition")
        expected_hash = conn.execute(
            "SELECT key_hash FROM students WHERE id = ?", (student["id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    copy = backup.create_database_backup(settings)
    restored = sqlite3.connect(copy)
    try:
        row = restored.execute(
            "SELECT key_hash, key_issued_by, key_issued_at FROM students"
        ).fetchone()
        assert row[0] == expected_hash
        assert row[1] == "transition"
        assert row[2]
        assert key not in str(row)
    finally:
        restored.close()


def test_client_ip_ignores_untrusted_forwarded_header():
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/login",
        "headers": [(b"x-forwarded-for", b"203.0.113.99")],
        "client": ("127.0.0.1", 8000),
    })
    assert client_ip(request) == "127.0.0.1"


def _logout(client):
    page = client.get("/")
    response = client.post(
        "/logout", data={"csrf": csrf_from(page)}, follow_redirects=False
    )
    assert response.status_code == 303


def test_existing_student_uses_key_and_empty_first_step_is_not_an_error(client):
    login_student(client, "key_login_student")
    key = client._test_student_keys["key_login_student"]
    _logout(client)
    page = client.get("/login")
    response = client.post(
        "/login", data={"nick": "key_login_student", "csrf": csrf_from(page)}
    )
    assert response.status_code == 200
    assert 'name="personal_key"' in response.text
    assert 'autocomplete="current-password"' in response.text
    assert 'data-password-toggle="personal-key"' in response.text
    assert 'aria-controls="personal-key"' in response.text
    assert 'aria-label="Показать личный ключ"' in response.text
    assert 'id="personal-key" name="personal_key" type="password"' in response.text
    conn = client.app.state.db_connect()
    try:
        student = conn.execute(
            "SELECT key_failed_count FROM students WHERE nick_norm = 'key_login_student'"
        ).fetchone()
        assert student["key_failed_count"] == 0
    finally:
        conn.close()
    response = client.post(
        "/login",
        data={
            "nick": "key_login_student", "personal_key": "неправильный",
            "csrf": csrf_from(response),
        },
    )
    assert response.status_code == 403
    assert "Ник или ключ не подходят" in response.text
    response = client.post(
        "/login",
        data={
            "nick": "key_login_student", "personal_key": key,
            "csrf": csrf_from(response),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_fifth_wrong_key_returns_423_on_login(client):
    login_student(client, "locked_login_student")
    key = client._test_student_keys["locked_login_student"]
    _logout(client)
    page = client.get("/login")
    response = client.post(
        "/login", data={"nick": "locked_login_student", "csrf": csrf_from(page)}
    )
    for attempt in range(1, 6):
        response = client.post(
            "/login",
            data={
                "nick": "locked_login_student", "personal_key": "неправильный",
                "csrf": csrf_from(response),
            },
        )
        assert response.status_code == (423 if attempt == 5 else 403)
    response = client.post(
        "/login",
        data={
            "nick": "locked_login_student", "personal_key": key,
            "csrf": csrf_from(response),
        },
    )
    assert response.status_code == 423


def test_registration_card_is_private_expires_and_can_be_closed(client):
    response = login_student(client, "card_student")
    card_path = response.headers["location"]
    key = client._test_student_keys["card_student"]
    assert key not in card_path
    card = client.get(card_path)
    assert card.status_code == 200
    assert key in card.text
    assert "Я сохранил ключ" in card.text
    assert card.headers["cache-control"] == "no-store"
    assert card.headers["referrer-policy"] == "no-referrer"
    assert client.get(card_path).status_code == 200
    conn = client.app.state.db_connect()
    try:
        student = conn.execute(
            "SELECT key_hash FROM students WHERE nick_norm = 'card_student'"
        ).fetchone()
        assert student["key_hash"] != key
        assert key not in str(conn.execute("SELECT * FROM audit_log").fetchall())
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'student_registered'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    login_student(client, "other_card_student")
    assert client.get(card_path).status_code == 404
    login_student(client, "card_student")
    card = client.get(card_path)
    assert card.status_code == 200
    response = client.post(
        f"{card_path}/saved",
        data={"csrf": csrf_from(card)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.get(card_path).status_code == 404


def test_access_card_store_expires_after_ten_minutes():
    now = [100.0]
    store = AccessCardStore(clock=lambda: now[0])
    actor = SimpleNamespace(is_admin=False, student_id=7)
    card_id = store.create(
        nick="student", key="липа-ракета-гром", audience="student", student_id=7
    )
    assert store.get(card_id, actor)["key"] == "липа-ракета-гром"
    now[0] += 601
    assert store.get(card_id, actor) is None


def test_cabinet_changes_key_and_renews_only_current_session(client):
    login_student(client, "change_key_student")
    old_key = client._test_student_keys["change_key_student"]
    cabinet = client.get("/cabinet")
    assert "Текущий ключ" in cabinet.text
    conn = client.app.state.db_connect()
    try:
        old_token = conn.execute(
            "SELECT login_token FROM students WHERE nick_norm = 'change_key_student'"
        ).fetchone()[0]
    finally:
        conn.close()
    response = client.post(
        "/cabinet/key",
        data={"csrf": csrf_from(cabinet), "current_key": "неправильный"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/cabinet"
    old_cookie = client.cookies.get("session")
    cabinet = client.get("/cabinet")
    response = client.post(
        "/cabinet/key",
        data={"csrf": csrf_from(cabinet), "current_key": old_key},
        follow_redirects=False,
    )
    assert response.status_code == 303
    card_path = response.headers["location"]
    card = client.get(card_path)
    assert card.status_code == 200
    match = re.search(r"Ключ: ([^<\n]+)", card.text)
    assert match and match.group(1).strip() != old_key
    with TestClient(client.app, base_url="http://testserver") as old_client:
        old_client.cookies.set("session", old_cookie)
        response = old_client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    assert client.get("/cabinet").status_code == 200
    conn = client.app.state.db_connect()
    try:
        student = conn.execute(
            "SELECT login_token, key_hash FROM students WHERE nick_norm = 'change_key_student'"
        ).fetchone()
        assert student["login_token"] != old_token
        assert not personal_keys.verify_key(old_key, student["key_hash"])
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'key_changed'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_transition_session_can_receive_first_key_without_current_key(client):
    login_student(client, "transition_key_student")
    conn = client.app.state.db_connect()
    try:
        conn.execute(
            "UPDATE students SET key_hash = NULL, key_issued_at = NULL, "
            "key_issued_by = NULL WHERE nick_norm = 'transition_key_student'"
        )
    finally:
        conn.close()
    cabinet = client.get("/cabinet")
    assert "без него вы не сможете войти с другого устройства" in cabinet.text
    assert 'name="current_key"' not in cabinet.text
    response = client.post(
        "/cabinet/key",
        data={"csrf": csrf_from(cabinet)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/card/")
    assert client.get(response.headers["location"]).status_code == 200
    conn = client.app.state.db_connect()
    try:
        student = conn.execute(
            "SELECT key_issued_by, key_hash FROM students WHERE nick_norm = 'transition_key_student'"
        ).fetchone()
        assert student["key_issued_by"] == "transition"
        assert student["key_hash"]
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'key_issued_transition'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
