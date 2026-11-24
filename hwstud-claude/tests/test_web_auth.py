from __future__ import annotations

import re

from app.db import connect

from .conftest import csrf_from, login_admin, login_student


def test_student_first_and_repeat_login(client, settings):
    response = client.get("/login")
    assert response.status_code == 200
    assert "ФИО" in response.text
    response = login_student(client, "Ivan_2")
    assert response.headers["location"] == "/"
    response = client.get("/")
    assert "Ivan_2" in response.text
    assert "Группа 1" in response.text
    assert "этап 1" in response.text.lower()
    csrf = csrf_from(response)
    response = client.post("/logout", data={"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    login_student(client, "ivan_2")
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1
    finally:
        conn.close()


def test_invalid_nick_and_csrf(client):
    response = client.get("/login")
    csrf = csrf_from(response)
    response = client.post("/login", data={"nick": "Иван", "csrf": csrf})
    assert response.status_code == 400
    response = client.post("/login", data={"nick": "valid_nick", "csrf": "wrong"})
    assert response.status_code == 403


def test_group_selection_allows_returning_to_nick_edit(client):
    response = client.get("/login")
    response = client.post(
        "/login",
        data={"nick": "new_student", "csrf": csrf_from(response)},
    )
    assert response.status_code == 200
    assert "Войти и создать ник" in response.text
    assert '<a class="button secondary" href="/login">Изменить ник</a>' in response.text


def test_blocked_student_session_is_rejected(client, settings):
    login_student(client)
    conn = connect(settings)
    try:
        conn.execute("UPDATE students SET blocked = 1")
    finally:
        conn.close()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_login_and_role_separation(client):
    response = login_admin(client, "wrong")
    assert response.status_code == 403
    response = login_admin(client)
    assert response.status_code == 303
    assert client.get("/admin").status_code == 200
    response = client.get("/help?view=teacher")
    assert response.status_code == 200
    assert "Преподавателю" in response.text
    response = client.get("/help?view=student")
    assert response.status_code == 200
    assert "Помощь администратора" not in response.text


def test_student_cannot_open_teacher_help_or_admin(client):
    login_student(client)
    assert client.get("/help?view=teacher").status_code == 404
    response = client.get("/help")
    assert response.status_code == 200
    assert "Открыт вариант:" not in response.text
    assert client.get("/admin").status_code == 404


def test_admin_rate_limit(client):
    for _ in range(5):
        response = login_admin(client, "wrong")
        assert response.status_code == 403
    response = login_admin(client, "wrong")
    assert response.status_code == 429


def test_test_email_is_only_enqueued(client, settings):
    login_admin(client)
    response = client.get("/admin/settings")
    response = client.post(
        "/admin/settings/test-email",
        data={"csrf": csrf_from(response)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        job = conn.execute("SELECT * FROM jobs").fetchone()
        assert job["kind"] == "mail"
        assert job["status"] == "queued"
        assert "пробное письмо" in job["payload_json"]
    finally:
        conn.close()


def test_proxyapi_config_is_editable(client, settings):
    login_admin(client)
    response = client.get("/admin/settings/proxyapi")
    response = client.post(
        "/admin/settings/proxyapi/config",
        data={
            "csrf": csrf_from(response), "balance_low": "400", "balance_critical": "120",
            "reserve_factor": "1.5", "image_tokens": "1600", "price_grading_in": "260",
            "price_grading_out": "1300", "price_aux_in": "30", "price_aux_out": "150",
            "billing_url": "https://proxyapi.ru/billing",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assert conn.execute("SELECT value FROM settings WHERE key = 'balance_low'").fetchone()[0] == "400.0"
        assert conn.execute("SELECT value FROM settings WHERE key = 'billing_url'").fetchone()[0] == "https://proxyapi.ru/billing"
    finally:
        conn.close()


def test_admin_navigation_has_one_overview_and_clean_help_switch(client):
    login_admin(client)
    response = client.get("/admin")
    navigation = re.search(r'<nav class="admin-nav".*?</nav>', response.text, re.S)
    assert navigation
    assert navigation.group(0).count(">Обзор<") == 1
    assert navigation.group(0).count('href="/admin/announcements"') == 1
    assert 'class="site-header teacher-header"' in response.text
    mobile = re.search(r'<nav id="mobile-menu".*?</nav>', response.text, re.S)
    assert mobile
    assert mobile.group(0).count('href="/admin/announcements"') == 1
    css = client.get("/static/css/app.css").text
    assert "@media(max-width:1399px)" in css
    assert ".teacher-header .admin-nav{display:none}" in css
    response = client.get("/help?view=student")
    assert "Вариант:" not in response.text
    assert 'href="?view=teacher"' in response.text


def test_student_navigation_has_responsive_menu(client):
    login_student(client, "responsive_student", 1)
    response = client.get("/")
    assert 'class="site-header student-header"' in response.text
    assert re.search(r'<nav class="process-nav".*?</nav>', response.text, re.S)
    mobile = re.search(r'<nav id="mobile-menu".*?</nav>', response.text, re.S)
    assert mobile
    assert 'href="/announcements"' in mobile.group(0)
    assert response.text.count('action="/logout"') == 2
    css = client.get("/static/css/app.css").text
    assert "@media(max-width:991px)" in css
    assert ".student-header .process-nav{display:none}" in css
    assert "flex-wrap:wrap" in css


def test_static_theme_and_public_about(client):
    response = client.get("/about")
    assert response.status_code == 200
    assert "ИИ предлагает" in response.text
    assert 'src="http' not in response.text
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    response = client.get("/health")
    assert response.json()["status"] == "ok"
