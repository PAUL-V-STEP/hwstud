from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app

from .conftest import csrf_from, login_admin


def test_default_admin_requires_change_and_new_password_works(settings):
    settings = replace(settings, admin_password_hash="")
    with TestClient(create_app(settings)) as client:
        response = login_admin(client, "admin")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/settings/password"
        response = client.get("/admin", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/settings/password"
        response = client.get("/admin/settings/password")
        assert "первый вход" in response.text
        assert 'class="back-link"' not in response.text
        assert 'class="mobile-logout"' in response.text
        response = client.post(
            "/admin/settings/password",
            data={
                "csrf": csrf_from(response), "current_password": "admin",
                "new_password": "short", "confirmation": "short",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        response = client.get("/admin/settings/password")
        response = client.post(
            "/admin/settings/password",
            data={
                "csrf": csrf_from(response), "current_password": "admin",
                "new_password": "New-Strong-Password-2026",
                "confirmation": "New-Strong-Password-2026",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.get("/admin").status_code == 200
        conn = connect(settings)
        try:
            stored = conn.execute(
                "SELECT value FROM settings WHERE key = 'admin_password_hash'"
            ).fetchone()["value"]
            assert stored.startswith("scrypt$")
            assert "New-Strong-Password-2026" not in stored
            assert conn.execute(
                "SELECT value FROM settings WHERE key = 'admin_password_changed'"
            ).fetchone()["value"] == "1"
        finally:
            conn.close()
        response = client.get("/admin")
        response = client.post("/logout", data={"csrf": csrf_from(response)})
        response = login_admin(client, "admin")
        assert response.status_code == 403
        response = login_admin(client, "New-Strong-Password-2026")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin"


def test_existing_custom_password_can_be_changed_from_settings(client, settings):
    login_admin(client)
    response = client.get("/admin/settings")
    assert "Сменить пароль" in response.text
    response = client.get("/admin/settings/password")
    response = client.post(
        "/admin/settings/password",
        data={
            "csrf": csrf_from(response), "current_password": "teacher-test",
            "new_password": "Another-Strong-Password-2026",
            "confirmation": "Another-Strong-Password-2026",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assert conn.execute(
            "SELECT value FROM settings WHERE key = 'admin_password_changed'"
        ).fetchone()["value"] == "1"
    finally:
        conn.close()
    response = client.get("/admin")
    client.post("/logout", data={"csrf": csrf_from(response)})
    assert login_admin(client, "teacher-test").status_code == 403
    assert login_admin(client, "Another-Strong-Password-2026").status_code == 303


def test_wrong_current_password_does_not_change_hash(client, settings):
    login_admin(client)
    response = client.get("/admin/settings/password")
    response = client.post(
        "/admin/settings/password",
        data={
            "csrf": csrf_from(response), "current_password": "wrong",
            "new_password": "Another-Strong-Password-2026",
            "confirmation": "Another-Strong-Password-2026",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assert not conn.execute(
            "SELECT 1 FROM settings WHERE key = 'admin_password_hash'"
        ).fetchone()
    finally:
        conn.close()
