from __future__ import annotations

import fitz

from app.auth import create_student
from app.db import connect, utc_now_iso

from .conftest import csrf_from, login_admin, login_student


def set_count(client, count: int):
    response = client.get("/admin/assignments")
    return client.post(
        "/admin/assignments/count",
        data={"csrf": csrf_from(response), "count": str(count)},
        follow_redirects=False,
    )


def test_reorder_changes_displayed_number_and_preserves_work(client, settings):
    conn = connect(settings)
    try:
        conn.execute(
            "UPDATE assignments SET title = 'Первое особое', condition = 'Первый текст' WHERE id = 1"
        )
        conn.execute(
            "UPDATE assignments SET title = 'Второе особое', condition = 'Второй текст' WHERE id = 2"
        )
        student, error = create_student(conn, "reorder_student", 1)
        assert not error
        conn.execute(
            "INSERT INTO submissions(student_id, assignment_id, attempt, file, mime, size, "
            "created_at, status, score, decided_at) VALUES (?, 1, 1, 'work.txt', 'text/plain', "
            "4, ?, 'accepted', 8, ?)",
            (student["id"], utc_now_iso(), utc_now_iso()),
        )
    finally:
        conn.close()
    login_admin(client)
    response = client.get("/admin/assignments")
    response = client.post(
        "/admin/assignments/2/move/up",
        data={"csrf": csrf_from(response)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assert conn.execute("SELECT position FROM assignments WHERE id = 1").fetchone()[0] == 2
        assert conn.execute("SELECT position FROM assignments WHERE id = 2").fetchone()[0] == 1
        assert conn.execute("SELECT title FROM assignments WHERE id = 1").fetchone()[0] == "Первое особое"
        saved_submission = conn.execute(
            "SELECT assignment_id, score FROM submissions WHERE student_id = ?", (student["id"],)
        ).fetchone()
        assert tuple(saved_submission) == (1, 8)
    finally:
        conn.close()
    page = client.get("/admin/assignments").text
    assert page.index('value="Второе особое"') < page.index('value="Первое особое"')
    login_student(client, "reorder_student", 1)
    results = client.get("/results?variant=1")
    assert results.status_code == 200
    assert "Задание 2" in results.text
    assert "8" in results.text
    response = client.get("/assignments/1/export.pdf")
    assert response.headers["content-disposition"] == 'attachment; filename="assignment-2.pdf"'
    with fitz.open(stream=response.content, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
    assert "Задание 2. Первое особое" in text
    assert "Первый текст" in text


def test_count_hides_restores_and_creates_assignments(client, settings):
    conn = connect(settings)
    try:
        conn.execute(
            "UPDATE assignments SET title = 'Сохранённое название', "
            "condition = 'Сохранённое условие' WHERE id = 1"
        )
    finally:
        conn.close()
    login_admin(client)
    response = set_count(client, 3)
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 5
        assert conn.execute(
            "SELECT value FROM settings WHERE key = 'assignments_count'"
        ).fetchone()[0] == "3"
    finally:
        conn.close()
    admin_page = client.get("/admin/assignments").text
    assert "Сохранённое название" in admin_page
    assert 'name="title" value="Задание 4"' not in admin_page
    login_student(client, "count_student", 1)
    student_page = client.get("/assignments").text
    assert 'action="/assignments/3/submit"' in student_page
    assert 'action="/assignments/4/submit"' not in student_page
    assert "0 / 3 принято" in client.get("/").text
    assert client.get("/assignments/4/export.pdf").status_code == 404
    assert 'href="/results?variant=5"' not in client.get("/results").text
    assert "Общее задание по всему курсу" not in client.get("/messages").text

    login_admin(client)
    response = set_count(client, 7)
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 7
        assert conn.execute(
            "SELECT title FROM assignments WHERE position = 1"
        ).fetchone()[0] == "Сохранённое название"
        assert conn.execute(
            "SELECT condition FROM assignments WHERE position = 1"
        ).fetchone()[0] == "Сохранённое условие"
        assert [row[0] for row in conn.execute(
            "SELECT position FROM assignments ORDER BY position"
        )] == [1, 2, 3, 4, 5, 6, 7]
    finally:
        conn.close()
    login_student(client, "count_student", 1)
    student_page = client.get("/assignments").text
    assert 'action="/assignments/7/submit"' in student_page
    assert client.get("/assignments/7/export.pdf").status_code == 200

    login_admin(client)
    set_count(client, 0)
    assert "Задания отключены" in client.get("/admin/assignments").text
    login_student(client, "count_student", 1)
    assert "Задания пока не добавлены" in client.get("/assignments").text
    assert "0 / 0 принято" in client.get("/").text
    assert client.get("/results?variant=sum").status_code == 200
    assert client.get("/messages").status_code == 200

    login_admin(client)
    set_count(client, 5)
    page = client.get("/admin/assignments").text
    assert "Сохранённое название" in page
    assert "Общее задание по всему курсу" in page
    assert 'name="title" value="Задание 6"' not in page
