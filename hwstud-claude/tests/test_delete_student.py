from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth import create_student
from app.db import connect

from .conftest import csrf_from, login_admin, login_student


def test_delete_nick_with_confirmation_and_free_reuse(client, app, settings):
    login_student(client, "delete_me", 1)
    response = client.get("/assignments")
    response = client.post(
        "/assignments/1/submit",
        data={"csrf": csrf_from(response)},
        files={"work": ("work.txt", b"Work for deletion", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.get("/messages")
    response = client.post(
        "/messages",
        data={"csrf": csrf_from(response), "body": "Message for deletion"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        other, error = create_student(conn, "other_student", 1)
        assert not error
        original = conn.execute("SELECT * FROM submissions WHERE student_id = 1").fetchone()
        original_file = settings.data_dir / original["file"]
        assert original_file.is_file()
        prepared = list((settings.data_dir / "tmp").glob("*.json"))
        assert prepared
    finally:
        conn.close()

    with TestClient(app) as admin:
        login_admin(admin)
        response = admin.get("/admin/students/1/delete")
        assert response.status_code == 200
        assert "delete_me" in response.text
        response = admin.post(
            "/admin/students/1/delete",
            data={"csrf": csrf_from(response)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        conn = connect(settings)
        try:
            assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 2
        finally:
            conn.close()
        response = admin.get("/admin/students/1/delete")
        response = admin.post(
            "/admin/students/1/delete",
            data={"csrf": csrf_from(response), "confirm_delete": "1"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert not original_file.exists()
    assert all(not path.exists() for path in prepared)
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1
        assert conn.execute("SELECT nick FROM students").fetchone()[0] == "other_student"
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind IN ('grading', 'mail')").fetchone()[0] == 0
        reused, error = create_student(conn, "delete_me", 1)
        assert reused is not None and error is None
        assert reused["id"] != 1
    finally:
        conn.close()
