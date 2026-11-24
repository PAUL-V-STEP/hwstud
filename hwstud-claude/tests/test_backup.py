from __future__ import annotations

import json
import sqlite3
import tarfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from app.auth import create_student
from app.db import connect, migrate, utc_now_iso
from app.services import backup
from app.worker import Worker, claim_next_job

from .conftest import csrf_from, login_admin, login_student


def test_backup_archive_check_and_restore(settings):
    migrate(settings)
    conn = connect(settings)
    try:
        create_student(conn, "backup_student", 1)
    finally:
        conn.close()
    database = backup.create_database_backup(settings)
    assert database.is_file()
    archive = backup.create_site_archive(settings)
    with tarfile.open(archive, "r:gz") as source:
        names = source.getnames()
        assert "data/hwstud.db" in names
        assert not any(name.startswith("data/uploads") for name in names)
    result = backup.check_latest_archive(settings)
    assert result["status"] == "ok"
    assert result["counts"]["students"] == 1
    restore = settings.data_dir / "tmp" / "restore-test"
    restore.mkdir()
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(restore, filter="data")
    conn = sqlite3.connect(restore / "data" / "hwstud.db")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1
    finally:
        conn.close()


def test_works_archive_replacement_and_temp_delete(settings):
    migrate(settings)
    conn = connect(settings)
    try:
        student, _ = create_student(conn, "archive_student", 1)
        path = settings.data_dir / "uploads" / "one.txt"
        path.parent.mkdir(parents=True)
        path.write_text("work", encoding="utf-8")
        conn.execute(
            "INSERT INTO submissions(student_id, assignment_id, attempt, file, mime, size, created_at, status) "
            "VALUES (?, 1, 1, 'uploads/one.txt', 'text/plain', 4, ?, 'queued')",
            (student["id"], utc_now_iso()),
        )
    finally:
        conn.close()
    first = backup.create_works_archive(settings)
    with zipfile.ZipFile(first) as archive:
        assert "uploads/one.txt" in archive.namelist()
        assert "manifest.csv" in archive.namelist()
    second = backup.create_works_archive(settings)
    assert second != first
    assert not first.exists()
    conn = connect(settings)
    try:
        job = conn.execute("SELECT * FROM jobs WHERE kind = 'delete_temp' ORDER BY id DESC").fetchone()
        payload = json.loads(job["payload_json"])
    finally:
        conn.close()
    backup.delete_temporary_file(settings, payload["path"])
    assert not second.exists()


def test_double_confirmation_and_complete_student_deletion(client, settings):
    login_student(client, "delete_student", 1)
    response = client.get("/assignments")
    response = client.post(
        "/assignments/1/submit",
        data={"csrf": csrf_from(response)},
        files={"work": ("work.txt", b"work", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    backup.create_site_archive(settings)
    login_admin(client)
    response = client.get("/admin/data/delete")
    response = client.post(
        "/admin/data/delete",
        data={"csrf": csrf_from(response), "confirm_first": "1"},
    )
    assert "Окончательное подтверждение" in response.text
    response = client.post(
        "/admin/data/delete/confirm",
        data={"csrf": csrf_from(response), "token": _token_from_session(client)},
        follow_redirects=False,
    )
    # Неправильная фраза не запускает удаление.
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1
    finally:
        conn.close()
    # Второй полный проход с правильной фразой.
    response = client.get("/admin/data/delete")
    response = client.post(
        "/admin/data/delete",
        data={"csrf": csrf_from(response), "confirm_first": "1"},
    )
    response = client.post(
        "/admin/data/delete/confirm",
        data={
            "csrf": csrf_from(response), "token": _token_from_session(client), "confirmation": "УДАЛИТЬ",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert backup.student_deletion_active(settings)
    assert client.get("/admin").status_code == 503
    response = client.get("/admin/data/deletion")
    assert response.status_code == 200
    assert 'class="back-link"' not in response.text
    assert "Запущенное удаление нельзя отменить" in response.text
    job = claim_next_job(settings)
    assert job["kind"] == "delete_students"
    worker = Worker(settings)
    import asyncio
    asyncio.run(worker._run_job(dict(job)))
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT started_at FROM maintenance").fetchone()[0] is None
        assert conn.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone()[0] == "done"
        assert not conn.execute("SELECT 1 FROM audit_log WHERE actor = 'delete_student'").fetchone()
    finally:
        conn.close()
    assert not list((settings.data_dir / "uploads").rglob("*.txt"))
    cleanup_archives = list((settings.data_dir / "archives").glob("*-cleanup.tar.gz"))
    assert cleanup_archives
    with tarfile.open(cleanup_archives[0], "r:gz") as archive:
        archive.extractall(settings.data_dir / "tmp" / "after-delete", filter="data")
    conn = sqlite3.connect(settings.data_dir / "tmp" / "after-delete" / "data" / "hwstud.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0
    finally:
        conn.close()
    assert not backup.student_deletion_active(settings)
    response = client.get("/admin/data/deletion")
    assert 'class="back-link" href="/admin/data"' in response.text


def _token_from_session(client) -> str:
    # Токен берём из HTML второго шага, а не из подписанной cookie.
    response = client.get("/admin/data/delete")
    # Текущий токен уже находится в сессии, поэтому повторяем первый шаг и читаем скрытое поле.
    import re
    response = client.post(
        "/admin/data/delete",
        data={"csrf": csrf_from(response), "confirm_first": "1"},
    )
    match = re.search(r'name="token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_resume_after_failure_from_saved_step(settings):
    migrate(settings)
    conn = connect(settings)
    try:
        conn.execute("UPDATE maintenance SET started_at = ?, deletion_step = 'db_done' WHERE id = 1", (utc_now_iso(),))
    finally:
        conn.close()
    backup.resume_student_deletion(settings)
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'delete_students'").fetchone()[0] == 1
    finally:
        conn.close()


def test_schedule_uses_internal_daily_and_sunday_slots(settings):
    migrate(settings)
    # 22.11.2026 — воскресенье; 01:30 UTC = 04:30 МСК.
    now = datetime(2026, 11, 22, 1, 30, tzinfo=UTC)
    operations = backup.run_due_maintenance(settings, now)
    assert operations == ["daily_backup", "weekly_archive", "archive_check", "cleanup"]
    assert backup.run_due_maintenance(settings, now) == []
