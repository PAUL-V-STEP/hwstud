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


def login_student(client: TestClient, nick: str = "student_1", group_id: int = 1):
    response = client.get("/login")
    response = client.post(
        "/login",
        data={"nick": nick, "csrf": csrf_from(response)},
        follow_redirects=False,
    )
    if response.status_code == 200:
        response = client.post(
            "/login",
            data={
                "nick": nick,
                "group_id": group_id,
                "csrf": csrf_from(response),
            },
            follow_redirects=False,
        )
    assert response.status_code == 303, response.text
    return response


def login_admin(client: TestClient, password: str = "teacher-test"):
    response = client.get("/admin/login")
    return client.post(
        "/admin/login",
        data={"password": password, "csrf": csrf_from(response)},
        follow_redirects=False,
    )
