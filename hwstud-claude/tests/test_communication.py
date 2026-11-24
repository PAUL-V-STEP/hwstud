from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.db import connect, migrate, utc_now_iso
from app.services import communication

from .conftest import csrf_from, login_admin, login_student


def create_announcement(client, **overrides):
    login_admin(client)
    response = client.get("/admin/announcements")
    data = {
        "csrf": csrf_from(response),
        "title": "Важное объявление",
        "body": "Текст объявления",
        "kind": "important",
        "publish_now": "1",
    }
    data.update(overrides)
    response = client.post("/admin/announcements", data=data, follow_redirects=False)
    assert response.status_code == 303
    return response


def test_announcement_groups_read_receipts_and_pinning(client, settings):
    create_announcement(client, selected_groups=["1"])
    login_student(client, "group_one", 1)
    response = client.get("/announcements")
    assert "Важное объявление" in response.text
    assert "Новое" in response.text
    home = client.get("/")
    assert 'class="counter"' in home.text
    response = client.get("/announcements/1")
    assert response.status_code == 200
    response = client.get("/announcements")
    assert "Прочитано" in response.text
    login_student(client, "group_two", 2)
    response = client.get("/announcements")
    assert "Важное объявление" not in response.text
    login_admin(client)
    response = client.get("/admin/announcements")
    assert "Группа 1: 1" in response.text
    conn = connect(settings)
    try:
        assert conn.execute("SELECT pinned FROM announcements WHERE id = 1").fetchone()[0] == 1
    finally:
        conn.close()


def test_scheduled_publication_without_immediate_checkbox(client, settings):
    login_admin(client)
    response = client.get("/admin/announcements")
    response = client.post(
        "/admin/announcements",
        data={
            "csrf": csrf_from(response), "title": "Будущее объявление", "body": "Позже",
            "kind": "normal", "publish_at": "2999-01-01T12:00",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        row = conn.execute("SELECT * FROM announcements").fetchone()
        assert row["publish_at"].startswith("2999-01-01")
        assert row["publish_at"].endswith("Z")
    finally:
        conn.close()
    login_student(client, "future_student", 1)
    assert "Пока объявлений нет" in client.get("/announcements").text


def test_announcement_has_no_optional_links_and_supports_schedule(client, settings):
    conn = connect(settings)
    try:
        conn.execute(
            "INSERT INTO lectures(slug, title, title_norm, status, updated_at) "
            "VALUES ('hidden-topic', 'Скрытая тема', 'скрытая тема', 'hidden', ?)",
            (utc_now_iso(),),
        )
    finally:
        conn.close()

    login_admin(client)
    form = client.get("/admin/announcements")
    assert 'name="link_type"' not in form.text
    assert 'name="lecture_id"' not in form.text
    assert 'name="assignment_id"' not in form.text
    assert "Скрытая тема" not in form.text
    response = client.post(
        "/admin/announcements",
        data={
            "csrf": csrf_from(form),
            "title": "Материал к занятию",
            "body": "Описание объявления",
            "kind": "normal",
            "publish_now": "1",
            "selected_groups": ["1"],
            "link_type": "lecture",
            "lecture_id": "",
            "assignment_id": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    conn = connect(settings)
    try:
        announcement = conn.execute("SELECT * FROM announcements").fetchone()
        assert announcement["link"] is None
    finally:
        conn.close()

    login_student(client, "link_student", 1)
    response = client.get("/announcements")
    assert "Материал к занятию" in response.text
    assert 'href="/lectures/hidden-topic"' not in response.text
    conn = connect(settings)
    try:
        conn.execute(
            "UPDATE announcements SET publish_at = '2999-01-01T00:00:00Z' WHERE id = 1"
        )
    finally:
        conn.close()
    response = client.get("/announcements")
    assert "Пока объявлений нет" in response.text


def test_message_thread_reply_close_and_reopen(client, settings):
    login_student(client, "message_student", 1)
    response = client.get("/messages")
    response = client.post(
        "/messages",
        data={"csrf": csrf_from(response), "body": "Как выполнить задание?", "ref_assignment": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        message = conn.execute("SELECT * FROM messages").fetchone()
        assert message["author"] == "student"
        assert conn.execute("SELECT closed_at FROM threads").fetchone()[0] is None
        mail = conn.execute("SELECT * FROM jobs WHERE kind = 'mail'").fetchone()
        payload = json.loads(mail["payload_json"])
        assert payload["category"] == "message"
        assert payload["message_id"] == message["id"]
        assert "message_student" in payload["subject"]
        assert "Задание" in payload["body"]
    finally:
        conn.close()
    login_admin(client)
    response = client.get("/admin/messages/1")
    assert "Как выполнить задание?" in response.text
    response = client.post(
        "/admin/messages/1/reply",
        data={"csrf": csrf_from(response), "body": "Начните с описания процесса"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE author = 'student' AND teacher_read_at IS NOT NULL").fetchone()[0] == 1
    finally:
        conn.close()
    login_student(client, "message_student", 1)
    response = client.get("/messages")
    assert "Начните с описания процесса" in response.text
    assert "Есть ответ" in response.text
    login_admin(client)
    response = client.get("/admin/messages/1")
    response = client.post(
        "/admin/messages/1/close",
        data={"csrf": csrf_from(response)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    login_student(client, "message_student", 1)
    response = client.get("/messages")
    response = client.post(
        "/messages",
        data={"csrf": csrf_from(response), "body": "Новый вопрос по той же теме"},
        follow_redirects=False,
    )
    conn = connect(settings)
    try:
        assert conn.execute("SELECT closed_at FROM threads").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
    finally:
        conn.close()


def test_message_accepts_empty_optional_references_and_has_expanded_field(client, settings):
    login_student(client, "empty_references", 1)
    page = client.get("/messages")
    assert 'class="form-stack message-form"' in page.text
    assert 'maxlength="2000" rows="8"' in page.text

    response = client.post(
        "/messages",
        data={
            "csrf": csrf_from(page),
            "body": "Общий вопрос без ссылки на материалы",
            "ref_lecture": "",
            "ref_slide": "",
            "ref_assignment": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    conn = connect(settings)
    try:
        message = conn.execute("SELECT * FROM messages").fetchone()
        assert message["body"] == "Общий вопрос без ссылки на материалы"
        assert message["ref_lecture"] is None
        assert message["ref_slide"] is None
        assert message["ref_assignment"] is None
    finally:
        conn.close()


def test_message_invalid_optional_reference_shows_form_error(client, settings):
    login_student(client, "invalid_reference", 1)
    page = client.get("/messages")
    response = client.post(
        "/messages",
        data={
            "csrf": csrf_from(page),
            "body": "Вопрос с некорректным номером слайда",
            "ref_lecture": "",
            "ref_slide": "не число",
            "ref_assignment": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert "Некорректное значение: слайд" in client.get("/messages").text

    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    finally:
        conn.close()


def test_message_limits_and_exam_availability(client, settings):
    login_student(client, "limited_student", 1)
    for index in range(10):
        response = client.get("/messages")
        response = client.post(
            "/messages",
            data={"csrf": csrf_from(response), "body": f"Вопрос {index}"},
            follow_redirects=False,
        )
        assert response.status_code == 303
    response = client.get("/messages")
    response = client.post(
        "/messages",
        data={"csrf": csrf_from(response), "body": "Лишний вопрос"},
        follow_redirects=False,
    )
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE author = 'student'").fetchone()[0] == 10
        conn.execute("UPDATE exam SET enabled = 1 WHERE id = 1")
    finally:
        conn.close()
    assert client.get("/messages").status_code == 200
    response = client.get("/messages")
    response = client.post(
        "/messages",
        data={"csrf": csrf_from(response), "body": "x" * 2001},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 10
    finally:
        conn.close()


def test_hourly_mode_creates_single_digest_and_marks_sent(settings):
    migrate(settings)
    login = login_student
    # Используем сервис напрямую, чтобы не выполнять SMTP.
    conn = connect(settings)
    try:
        conn.execute("UPDATE settings SET value = 'hourly' WHERE key = 'mail_mode'")
    finally:
        conn.close()
    # Клиент нужен только для создания ника через реальный маршрут.
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(settings)) as client:
        login(client, "digest_student", 1)
        response = client.get("/messages")
        response = client.post(
            "/messages",
            data={"csrf": csrf_from(response), "body": "Вопрос для сводки"},
            follow_redirects=False,
        )
        assert response.status_code == 303
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'mail'").fetchone()[0] == 0
    finally:
        conn.close()
    now = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    assert communication.enqueue_hourly_digest_if_due(settings, now)
    assert not communication.enqueue_hourly_digest_if_due(settings, now)
    conn = connect(settings)
    try:
        job = conn.execute("SELECT * FROM jobs WHERE kind = 'mail'").fetchone()
        payload = json.loads(job["payload_json"])
        assert payload["category"] == "message_digest"
        assert len(payload["message_ids"]) == 1
        communication.mark_mailed(settings, payload)
        assert conn.execute("SELECT mailed_at FROM messages").fetchone()[0] is not None
    finally:
        conn.close()


def test_teacher_menu_links_to_dialog_and_shows_unread(client, app, settings):
    login_student(client, "visible_question", 1)
    response = client.get("/messages")
    response = client.post(
        "/messages",
        data={"csrf": csrf_from(response), "body": "Вопрос должен быть виден преподавателю"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with TestClient(app) as admin:
        login_admin(admin)
        response = admin.get("/admin")
        assert 'href="/admin/messages"' in response.text
        assert "Вопросы студентов" in response.text
        assert 'class="counter">1</span>' in response.text
        response = admin.get("/admin/messages")
        assert response.status_code == 200
        assert "visible_question" in response.text
        response = admin.get("/admin/messages/1")
        assert response.status_code == 200
        assert 'action="/admin/messages/1/reply"' in response.text
        assert "Вопрос должен быть виден преподавателю" in response.text
        response = admin.get("/admin")
        assert 'class="counter">0</span>' not in response.text


def test_after_thirty_letters_new_message_waits_for_digest(client, settings):
    login_student(client, "threshold_student", 1)
    conn = connect(settings)
    try:
        for _ in range(30):
            conn.execute(
                "INSERT INTO messages(student_id, author, body, created_at, mailed_at) "
                "VALUES (1, 'student', 'Ранее отправлено', ?, ?)",
                (utc_now_iso(), utc_now_iso()),
            )
    finally:
        conn.close()
    response = client.get("/messages")
    response = client.post(
        "/messages",
        data={"csrf": csrf_from(response), "body": "После тридцати писем"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        latest = conn.execute("SELECT id FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        assert communication.queue_message_email(settings, latest["id"]) == "hourly"
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'mail'").fetchone()[0] == 0
    finally:
        conn.close()
