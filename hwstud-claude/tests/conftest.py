from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["SESSION_SECRET"] = "test-only-session-secret"
os.environ["ENABLE_WORKER"] = "false"

from app.auth import hash_password
from app.config import PROJECT_ROOT, load_settings
from app.main import create_app
from app.services import personal_keys
from app.services.registration import open_window

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def csrf_from(response) -> str:
    match = CSRF_RE.search(response.text)
    assert match, response.text[:500]
    return match.group(1)


@pytest.fixture
def settings(tmp_path: Path):
    return load_settings(
        base_dir=PROJECT_ROOT,
        overrides={
            "DATA_DIR": str(tmp_path / "data"),
            "APP_BASE_URL": "http://testserver",
            "SESSION_SECRET": "test-only-session-secret",
            "ADMIN_PASSWORD_HASH": hash_password("teacher-test", n=2**14),
            "ENABLE_WORKER": False,
            "PROXYAPI_KEY": "test-proxy-key",
            "SMTP_HOST": "smtp.test",
            "SMTP_USER": "teacher@test",
            "SMTP_PASSWORD": "test-password",
            "MAIL_FROM": "teacher@test",
            "MAIL_TO": "teacher@test",
        },
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


def open_registration(client: TestClient, group_id: int = 1) -> None:
    conn = client.app.state.db_connect()
    try:
        open_window(conn, [group_id])
    finally:
        conn.close()


def login_student(client: TestClient, nick: str = "student_1", group_id: int = 1):
    keys = getattr(client, "_test_student_keys", None)
    if keys is None:
        keys = {}
        client._test_student_keys = keys
    conn = client.app.state.db_connect()
    try:
        existing = conn.execute(
            "SELECT * FROM students WHERE nick_norm = ?", (nick.strip().lower(),)
        ).fetchone()
        if existing and not existing["key_hash"]:
            keys[nick.strip().lower()] = personal_keys.issue_key(
                conn, existing["id"], "transition"
            )
    finally:
        conn.close()
    open_registration(client, group_id)
    response = client.get("/login")
    response = client.post(
        "/login",
        data={"nick": nick, "csrf": csrf_from(response)},
        follow_redirects=False,
    )
    if response.status_code == 200:
        data = {"nick": nick, "csrf": csrf_from(response)}
        if 'name="personal_key"' in response.text:
            key = keys.get(nick.strip().lower())
            assert key, "Тестовый ключ не выдан"
            data["personal_key"] = key
        else:
            data["group_id"] = group_id
        response = client.post(
            "/login", data=data, follow_redirects=False,
        )
    if response.status_code == 303 and response.headers["location"].startswith("/card/"):
        card = client.get(response.headers["location"])
        match = re.search(r"Ключ: ([^<\n]+)", card.text)
        assert match, "Ключ не найден на карточке"
        keys[nick.strip().lower()] = match.group(1).strip()
    assert response.status_code == 303, response.text
    return response


def login_admin(client: TestClient, password: str = "teacher-test"):
    response = client.get("/admin/login")
    return client.post(
        "/admin/login",
        data={"password": password, "csrf": csrf_from(response)},
        follow_redirects=False,
    )
