from __future__ import annotations

import re

from app.db import connect

from .conftest import csrf_from, login_admin, login_student, open_registration


def test_student_first_and_repeat_login(client, settings):
    response = client.get("/login")
    assert response.status_code == 200
    assert 'name="nick"' in response.text
    response = login_student(client, "Ivan_2")
    assert response.headers["location"].startswith("/card/")
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


def test_student_login_has_no_nick_hint(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert 'id="nick-hint"' not in response.text
    assert 'aria-describedby="nick-hint"' not in response.text
    assert "3–24 символа" not in response.text
    assert "Войдите по нику" not in response.text
    assert "Пароль, ФИО и почта не нужны" not in response.text
    assert 'autocomplete="username"' not in response.text
    assert response.text.count('autocomplete="off"') == 2
    assert 'autocorrect="off"' in response.text
    assert 'autocapitalize="none"' in response.text


def test_invalid_nick_and_csrf(client):
    response = client.get("/login")
    csrf = csrf_from(response)
    response = client.post("/login", data={"nick": "Иван", "csrf": csrf})
    assert response.status_code == 400
    response = client.post("/login", data={"nick": "valid_nick", "csrf": "wrong"})
    assert response.status_code == 403


def test_group_selection_allows_returning_to_nick_edit(client):
    open_registration(client)
    response = client.get("/login")
    response = client.post(
        "/login",
        data={"nick": "new_student", "csrf": csrf_from(response)},
    )
    assert response.status_code == 200
    assert "Войти и создать ник" in response.text
    assert '<a class="button secondary" href="/login">Изменить ник</a>' in response.text


def test_login_pages_have_no_author_or_developer_links(client):
    for path in ("/login", "/admin/login"):
        response = client.get(path)
        assert response.status_code == 200
        assert 'href="/about"' not in response.text
        assert "https://ai-and-hi.pplx.app" not in response.text
        assert "О курсе" not in response.text
        assert '<div class="author-brand">' in response.text
        assert '<a class="author-brand"' not in response.text
        assert response.text.count('class="logo-light"') == 1
        assert response.text.count('class="logo-dark"') == 1


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


def test_admin_login_has_no_initial_password_guidance(client):
    response = client.get("/admin/login")
    assert response.status_code == 200
    assert "При первом входе используйте пароль" not in response.text
    assert "Если пароль уже настроен" not in response.text
    assert 'name="password"' in response.text


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


def test_form_scroll_preservation_script_is_versioned_and_scoped(client):
    page = client.get("/about")
    assert 'src="/static/js/app.js?v=20261125-4"' in page.text
    assert 'src="/static/js/registration.js?v=20261125-1"' in page.text
    script = client.get("/static/js/app.js").text
    assert "sessionStorage.setItem(formScrollKey" in script
    assert "parsed.path === window.location.pathname" in script
    assert "window.scrollTo(0, savedFormScroll)" in script
    assert "root.style.scrollBehavior = 'auto'" in script
    assert "form.matches('[data-upload-form]')" in script
    assert "document.querySelectorAll('[data-password-toggle]')" in script
    assert "input.type = visible ? 'text' : 'password'" in script
    assert "button.hidden = false" in script
    assert "const subject = button.dataset.label || 'личный ключ'" in script
    assert "`Скрыть ${subject}`" in script


def test_static_theme_and_public_about(client):
    response = client.get("/about")
    assert response.status_code == 200
    assert "ИИ предлагает" in response.text
    assert 'src="http' not in response.text
    assert response.text.count('class="about-author-logo logo-light"') == 1
    assert response.text.count('class="about-author-logo logo-dark"') == 1
    assert 'class="author-brand"' not in response.text
    assert 'app.css?v=20261125-2' in response.text
    css_response = client.get("/static/css/app.css")
    assert css_response.status_code == 200
    assert ".password-field{position:relative}" in css_response.text
    assert ".password-toggle{position:absolute" in css_response.text
    assert "width:44px;height:44px" in css_response.text
    assert ".author-brand img{display:block;width:192px;height:64px" in css_response.text
    assert ".about-author-logo{display:block;width:240px;height:80px" in css_response.text
    assert "@media(min-width:768px){.author-brand img{width:144px;height:48px}.about-author-logo{width:180px;height:60px}}" in css_response.text
    assert "img.logo-dark{display:none!important}" in css_response.text
    assert ':root[data-theme="dark"] img.logo-light{display:none!important}' in css_response.text
    assert ':root[data-theme="dark"] img.logo-dark{display:block!important}' in css_response.text
    assert css_response.text.index("img.logo-dark{display:none!important}") > css_response.text.index(".author-brand img{")
    assert client.get("/static/js/app.js").status_code == 200
    response = client.get("/health")
    assert response.json()["status"] == "ok"


def test_student_pages_have_one_logo_location(client):
    login_student(client)
    response = client.get("/")
    assert response.status_code == 200
    assert response.text.count('<a class="author-brand"') == 1
    assert response.text.count('class="logo-light"') == 1
    assert response.text.count('class="logo-dark"') == 1
    assert response.text.count("about-author-logo") == 0
