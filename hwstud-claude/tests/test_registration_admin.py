from __future__ import annotations

import re

from app.auth import create_student

from .conftest import csrf_from, login_admin, login_student
from .test_submissions import upload_txt


def test_admin_registration_windows_status_and_access(client):
    response = client.get("/admin/registration", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    login_admin(client)
    page = client.get("/admin/registration")
    assert page.status_code == 200
    assert "Группы и окна регистрации" in page.text
    assert "0 из 20" in page.text
    response = client.post(
        "/admin/registration/open",
        data={"csrf": csrf_from(page), "group_ids": ["1"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    status = client.get("/admin/registration/status")
    assert status.headers["cache-control"] == "no-store"
    assert status.json()["windows"][0]["group_id"] == 1
    assert status.json()["windows"][0]["seconds_left"] > 0
    dashboard = client.get("/admin")
    assert dashboard.text.count('href="/admin/registration"') == 2
    assert "data-registration-countdown" in dashboard.text
    page = client.get("/admin/registration")
    response = client.post(
        "/admin/registration/extend",
        data={"csrf": csrf_from(page), "group_id": 1},
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.post(
        "/admin/registration/close",
        data={"csrf": csrf_from(page), "group_id": 1},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/admin/registration/status").json()["windows"] == []
    page = client.get("/admin/registration")
    client.post(
        "/admin/registration/open",
        data={"csrf": csrf_from(page), "group_ids": ["1", "2"]},
    )
    page = client.get("/admin/registration")
    response = client.post(
        "/admin/registration/close",
        data={"csrf": csrf_from(page), "all": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/admin/registration/status").json()["windows"] == []
    page = client.get("/admin/registration")
    assert client.post(
        "/admin/registration/open", data={"group_ids": ["1"]}
    ).status_code == 403


def test_live_new_students_and_quick_delete_rules(client, settings):
    login_student(client, "quick_delete_student")
    login_admin(client)
    page = client.get("/admin/registration")
    assert "quick_delete_student" in page.text
    conn = client.app.state.db_connect()
    try:
        student_id = conn.execute(
            "SELECT id FROM students WHERE nick_norm = 'quick_delete_student'"
        ).fetchone()[0]
    finally:
        conn.close()
    response = client.post(
        "/admin/registration/delete",
        data={"csrf": csrf_from(page), "student_id": student_id},
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/admin/students/{student_id}/delete"
    page = client.get("/admin/registration")
    response = client.post(
        "/admin/registration/delete",
        data={
            "csrf": csrf_from(page), "student_id": student_id, "confirmed": "1"
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = client.app.state.db_connect()
    try:
        assert not conn.execute(
            "SELECT 1 FROM students WHERE id = ?", (student_id,)
        ).fetchone()
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'student_deleted_quick'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    login_student(client, "student_with_work")
    upload_txt(client)
    login_admin(client)
    page = client.get("/admin/registration")
    conn = client.app.state.db_connect()
    try:
        student_id = conn.execute(
            "SELECT id FROM students WHERE nick_norm = 'student_with_work'"
        ).fetchone()[0]
    finally:
        conn.close()
    response = client.post(
        "/admin/registration/delete",
        data={
            "csrf": csrf_from(page), "student_id": student_id, "confirmed": "1"
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/admin/students/{student_id}/delete"
    conn = client.app.state.db_connect()
    try:
        assert conn.execute(
            "SELECT 1 FROM students WHERE id = ?", (student_id,)
        ).fetchone()
    finally:
        conn.close()


def test_admin_creates_and_resets_key_without_registration_window(client):
    login_admin(client)
    page = client.get("/admin/groups")
    response = client.post(
        "/admin/students/create",
        data={"csrf": csrf_from(page), "nick": "admin_created", "group_id": 1},
        follow_redirects=False,
    )
    assert response.status_code == 303
    card_path = response.headers["location"]
    card = client.get(card_path)
    assert "admin_created" in card.text
    assert card.headers["cache-control"] == "no-store"
    old_key = re.search(r"Ключ: ([^<\n]+)", card.text).group(1).strip()
    conn = client.app.state.db_connect()
    try:
        student = conn.execute(
            "SELECT * FROM students WHERE nick_norm = 'admin_created'"
        ).fetchone()
        student_id = student["id"]
        old_token = student["login_token"]
        assert student["key_issued_by"] == "admin"
        assert student["key_hash"] != old_key
    finally:
        conn.close()
    groups = client.get("/admin/groups")
    assert "Сбросить ключ" in groups.text
    assert "преподаватель" in groups.text
    assert old_key not in groups.text
    response = client.post(
        f"/admin/students/{student_id}/reset-key",
        data={"csrf": csrf_from(groups)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    new_card = client.get(response.headers["location"])
    new_key = re.search(r"Ключ: ([^<\n]+)", new_card.text).group(1).strip()
    assert new_key != old_key
    conn = client.app.state.db_connect()
    try:
        student = conn.execute(
            "SELECT login_token, key_failed_count, key_locked_until FROM students WHERE id = ?",
            (student_id,),
        ).fetchone()
        assert student["login_token"] != old_token
        assert student["key_failed_count"] == 0
        assert student["key_locked_until"] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'key_reset'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_bulk_creation_reports_errors_and_downloads_txt(client):
    login_admin(client)
    page = client.get("/admin/groups")
    response = client.post(
        "/admin/students/bulk",
        data={
            "csrf": csrf_from(page), "group_id": 1,
            "nicks": "bulk_one\ninvalid nick\nbulk_two\nbulk_one",
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Скопировать все" in response.text
    assert "Скачать TXT" in response.text
    assert "invalid nick" in response.text
    assert "Такой ник уже занят" in response.text
    keys = re.findall(r"Ключ: ([^<\n]+)", response.text)
    assert len(keys) >= 2
    content = "\n".join(
        line for line in response.text.splitlines() if line.startswith("Ключ:")
    )
    response = client.post(
        "/admin/students/bulk/download",
        data={"csrf": csrf_from(response), "content": content},
    )
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.text == content
    conn = client.app.state.db_connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM students WHERE nick_norm IN ('bulk_one', 'bulk_two')"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_bulk_creation_respects_group_limit(client):
    login_admin(client)
    conn = client.app.state.db_connect()
    try:
        conn.execute("UPDATE groups SET max_members = 1 WHERE id = 1")
    finally:
        conn.close()
    page = client.get("/admin/groups")
    response = client.post(
        "/admin/students/bulk",
        data={
            "csrf": csrf_from(page), "group_id": 1,
            "nicks": "only_one\nover_limit",
        },
    )
    assert response.status_code == 200
    assert "В группе уже 20 участников" in response.text
    conn = client.app.state.db_connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1
    finally:
        conn.close()


def test_registration_script_is_local_and_scoped(client):
    login_admin(client)
    script = client.get("/static/js/registration.js")
    assert script.status_code == 200
    assert "/admin/registration/status" in script.text
    assert "window.setInterval(refresh, 5000)" in script.text
    assert "textContent" in script.text
    assert "innerHTML" not in script.text
    app_script = client.get("/static/js/app.js").text
    assert "form.dataset.confirm" in app_script
    assert "form.requestSubmit()" in app_script


def test_bulk_issue_keys_only_to_legacy_students(client):
    login_student(client, "already_keyed")
    login_admin(client)
    conn = client.app.state.db_connect()
    try:
        for nick in ("legacy_one", "legacy_two"):
            student, error = create_student(conn, nick, 1)
            assert student and not error
    finally:
        conn.close()
    page = client.get("/admin/groups")
    assert "Выдать ключи всем никам без ключа" in page.text
    response = client.post(
        "/admin/students/issue-keys",
        data={"csrf": csrf_from(page)},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "legacy_one" in response.text
    assert "legacy_two" in response.text
    assert "already_keyed" not in response.text
    assert len(re.findall(r"Ключ: ([^<\n]+)", response.text)) >= 2
    conn = client.app.state.db_connect()
    try:
        students = conn.execute(
            "SELECT key_hash, key_issued_by FROM students"
        ).fetchall()
        assert all(student["key_hash"] for student in students)
        assert {
            student["key_issued_by"] for student in students
            if student["key_issued_by"] == "transition"
        } == {"transition"}
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'key_issued_transition'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'keys_bulk_issued'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
