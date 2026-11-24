from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import connect, parse_utc, transaction, utc_now, utc_now_iso


def _timestamp(cleanup: bool = False) -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f") + ("-cleanup" if cleanup else "")


def _set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _check_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Копия базы не прошла integrity_check")
    finally:
        conn.close()


def create_database_backup(settings: Settings, *, cleanup: bool = False) -> Path:
    folder = settings.data_dir / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    if cleanup:
        target = folder / f"hwstud-{_timestamp(True)}.db"
    else:
        target = folder / f"hwstud-{utc_now().astimezone(settings.timezone):%Y%m%d}.db"
        if target.exists():
            return target
    temporary = target.with_suffix(".db.tmp")
    source = connect(settings)
    destination = sqlite3.connect(temporary)
    try:
        source.backup(destination)
        destination.close()
        source.close()
        _check_database(temporary)
        os.replace(temporary, target)
    finally:
        source.close()
        destination.close()
        temporary.unlink(missing_ok=True)
    if not cleanup:
        _prune(folder, "hwstud-*.db", 14, keep_suffix="-cleanup")
    return target


def create_site_archive(settings: Settings, *, cleanup: bool = False) -> Path:
    folder = settings.data_dir / "archives"
    folder.mkdir(parents=True, exist_ok=True)
    if cleanup:
        target = folder / f"site-{_timestamp(True)}.tar.gz"
    else:
        target = folder / f"site-{utc_now().astimezone(settings.timezone):%Y%m%d}.tar.gz"
        if target.exists():
            return target
    database = create_database_backup(settings, cleanup=cleanup)
    temporary = target.with_suffix(".tar.gz.tmp")
    with tarfile.open(temporary, "w:gz") as archive:
        archive.add(database, arcname="data/hwstud.db")
        lectures = settings.data_dir / "lectures"
        if lectures.exists():
            archive.add(lectures, arcname="data/lectures")
    with tarfile.open(temporary, "r:gz") as archive:
        archive.getmembers()
    os.replace(temporary, target)
    if not cleanup:
        _prune(folder, "site-*.tar.gz", 2, keep_suffix="-cleanup")
    return target


def check_latest_archive(settings: Settings) -> dict[str, Any]:
    (settings.data_dir / "tmp").mkdir(parents=True, exist_ok=True)
    folder = settings.data_dir / "archives"
    archives = sorted(
        (path for path in folder.glob("site-*.tar.gz") if "-cleanup" not in path.name),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    try:
        if not archives:
            raise ValueError("Нет архива для проверки")
        source = archives[0]
        with tempfile.TemporaryDirectory(prefix="archive-check-", dir=settings.data_dir / "tmp") as temporary:
            with tarfile.open(source, "r:gz") as archive:
                archive.extractall(temporary, filter="data")
            database = Path(temporary) / "data" / "hwstud.db"
            _check_database(database)
            conn = sqlite3.connect(database)
            try:
                counts = {
                    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("groups", "students", "lectures", "assignments")
                }
            finally:
                conn.close()
        result = {
            "at": utc_now_iso(),
            "archive": source.name,
            "status": "ok",
            "counts": counts,
        }
    except Exception as exc:
        result = {"at": utc_now_iso(), "status": "error", "error": str(exc)}
        _queue_archive_error(settings, str(exc))
    conn = connect(settings)
    try:
        with transaction(conn):
            _set_setting(conn, "last_archive_check", json.dumps(result, ensure_ascii=False))
    finally:
        conn.close()
    return result


def _queue_archive_error(settings: Settings, error: str) -> None:
    conn = connect(settings)
    try:
        with transaction(conn):
            payload = {
                "subject": "hwstud: ошибка проверки архива",
                "body": error,
            }
            conn.execute(
                "INSERT INTO jobs(kind, priority, payload_json, run_after) VALUES ('mail', 1, ?, ?)",
                (json.dumps(payload, ensure_ascii=False), utc_now_iso()),
            )
    finally:
        conn.close()


def create_works_archive(settings: Settings) -> Path:
    uploads = settings.data_dir / "uploads"
    files = sorted(path for path in uploads.rglob("*") if path.is_file()) if uploads.exists() else []
    if not files:
        raise ValueError("Загруженных работ пока нет")
    target = settings.data_dir / "tmp" / f"works-{_timestamp()}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    incomplete = target.with_suffix(".zip.tmp")
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT s.id, st.nick, g.name AS group_name, s.assignment_id, s.attempt, s.file "
            "FROM submissions s JOIN students st ON st.id = s.student_id "
            "JOIN groups g ON g.id = st.group_id ORDER BY s.id"
        ).fetchall()
    finally:
        conn.close()
    with zipfile.ZipFile(incomplete, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest = io.StringIO()
        writer = csv.writer(manifest)
        writer.writerow(("ID", "Группа", "Ник", "Задание", "Попытка", "Файл"))
        for row in rows:
            writer.writerow(
                (row["id"], row["group_name"], row["nick"], row["assignment_id"], row["attempt"], row["file"])
            )
        archive.writestr("manifest.csv", manifest.getvalue().encode("utf-8-sig"))
        for path in files:
            archive.write(path, arcname=f"uploads/{path.relative_to(uploads).as_posix()}")
    with zipfile.ZipFile(incomplete) as archive:
        if archive.testzip() is not None:
            raise ValueError("Архив работ не прошёл проверку CRC")
    conn = connect(settings)
    previous_path = None
    try:
        with transaction(conn, immediate=True):
            previous = conn.execute(
                "SELECT value FROM settings WHERE key = 'latest_works_archive'"
            ).fetchone()
            previous_path = previous["value"] if previous and previous["value"] else None
            os.replace(incomplete, target)
            relative = target.relative_to(settings.data_dir).as_posix()
            _set_setting(conn, "latest_works_archive", relative)
            conn.execute("UPDATE jobs SET status = 'cancelled' WHERE kind = 'delete_temp' AND status = 'queued'")
            run_after = (utc_now() + timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
            conn.execute(
                "INSERT INTO jobs(kind, priority, payload_json, run_after) "
                "VALUES ('delete_temp', 6, ?, ?)",
                (json.dumps({"path": relative}), run_after),
            )
    finally:
        conn.close()
    if previous_path:
        delete_temporary_file(settings, previous_path)
    return target


def delete_temporary_file(settings: Settings, relative: str) -> None:
    root = (settings.data_dir / "tmp").resolve()
    path = (settings.data_dir / relative).resolve()
    if path.is_relative_to(root) and path.name.startswith(("works-", "hwstud-")):
        path.unlink(missing_ok=True)


def cleanup(settings: Settings) -> None:
    from .lecture_import import expire_ready_imports

    expire_ready_imports(settings)
    imports = settings.data_dir / "import"
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT id FROM imports WHERE status IN ('saved', 'expired', 'cancelled', 'failed')"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        path = imports / str(row["id"])
        if path.exists():
            shutil.rmtree(path)
    _prune(settings.data_dir / "backups", "hwstud-*.db", 14, keep_suffix="-cleanup")
    _prune(settings.data_dir / "archives", "site-*.tar.gz", 2, keep_suffix="-cleanup")


def _prune(folder: Path, pattern: str, keep: int, keep_suffix: str) -> None:
    if not folder.exists():
        return
    files = sorted(
        (path for path in folder.glob(pattern) if keep_suffix not in path.name and path.suffix != ".tmp"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in files[keep:]:
        path.unlink(missing_ok=True)


def run_due_maintenance(settings: Settings, now: datetime | None = None) -> list[str]:
    now = now or utc_now()
    local = now.astimezone(settings.timezone)
    conn = connect(settings)
    operations: list[str] = []
    try:
        checks = (
            ("last_daily_backup_date", local.date().isoformat(), local.hour > 3 or (local.hour == 3 and local.minute >= 30), "daily_backup"),
            ("last_weekly_archive_date", local.date().isoformat(), local.weekday() == 6 and (local.hour > 4 or local.hour == 4 and local.minute >= 0), "weekly_archive"),
            ("last_archive_check_date", local.date().isoformat(), local.weekday() == 6 and (local.hour > 4 or local.hour == 4 and local.minute >= 15), "archive_check"),
            ("last_cleanup_date", local.date().isoformat(), local.hour > 4 or (local.hour == 4 and local.minute >= 30), "cleanup"),
        )
        for key, slot, due, operation in checks:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            if not due or (row and row["value"] == slot):
                continue
            if operation == "daily_backup":
                create_database_backup(settings)
            elif operation == "weekly_archive":
                create_site_archive(settings)
            elif operation == "archive_check":
                check_latest_archive(settings)
            else:
                cleanup(settings)
            with transaction(conn):
                _set_setting(conn, key, slot)
            operations.append(operation)
        return operations
    finally:
        conn.close()


def student_deletion_active(settings: Settings) -> bool:
    conn = connect(settings)
    try:
        row = conn.execute("SELECT started_at FROM maintenance WHERE id = 1").fetchone()
        return bool(row and row["started_at"])
    finally:
        conn.close()


def start_student_deletion(settings: Settings) -> int:
    conn = connect(settings)
    try:
        with transaction(conn, immediate=True):
            state = conn.execute("SELECT * FROM maintenance WHERE id = 1").fetchone()
            if state["started_at"]:
                raise ValueError("Удаление уже выполняется")
            conn.execute(
                "UPDATE maintenance SET started_at = ?, deletion_step = NULL WHERE id = 1",
                (utc_now_iso(),),
            )
            cursor = conn.execute(
                "INSERT INTO jobs(kind, priority, payload_json, run_after) "
                "VALUES ('delete_students', 2, '{}', ?)",
                (utc_now_iso(),),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def resume_student_deletion(settings: Settings) -> None:
    if not student_deletion_active(settings):
        return
    conn = connect(settings)
    try:
        active = conn.execute(
            "SELECT 1 FROM jobs WHERE kind = 'delete_students' AND status IN ('queued', 'running')"
        ).fetchone()
        if not active:
            conn.execute(
                "INSERT INTO jobs(kind, priority, payload_json, run_after) "
                "VALUES ('delete_students', 2, '{}', ?)",
                (utc_now_iso(),),
            )
    finally:
        conn.close()


def delete_student_data(settings: Settings) -> None:
    conn = connect(settings)
    try:
        state = conn.execute("SELECT * FROM maintenance WHERE id = 1").fetchone()
        step = state["deletion_step"]
    finally:
        conn.close()
    if not state["started_at"]:
        return
    if step is None:
        _delete_student_rows(settings)
        step = "db_deleted"
    if step == "db_deleted":
        conn = connect(settings)
        try:
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute(
                "UPDATE maintenance SET deletion_step = 'db_done' WHERE id = 1"
            )
        finally:
            conn.close()
        step = "db_done"
    if step == "db_done":
        create_database_backup(settings, cleanup=True)
        create_site_archive(settings, cleanup=True)
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE maintenance SET deletion_step = 'backup_done' WHERE id = 1"
            )
        finally:
            conn.close()
        step = "backup_done"
    if step == "backup_done":
        _delete_old_student_files(settings)
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE maintenance SET deletion_step = 'files_done' WHERE id = 1"
            )
        finally:
            conn.close()
        step = "files_done"
    if step == "files_done":
        conn = connect(settings)
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO audit_log(at, actor, action, object) "
                    "VALUES (?, 'system', 'student_data_deleted', NULL)",
                    (utc_now_iso(),),
                )
                conn.execute(
                    "UPDATE maintenance SET deletion_step = NULL, started_at = NULL WHERE id = 1"
                )
        finally:
            conn.close()


def _delete_student_rows(settings: Settings) -> None:
    conn = connect(settings)
    try:
        with transaction(conn, immediate=True):
            conn.execute(
                "UPDATE audit_log SET actor = 'удалённый студент', object = 'удалённый студент', "
                "details_json = 'удалённый студент', student_id = NULL WHERE student_id IS NOT NULL"
            )
            for table in (
                "review_requests", "announcement_reads", "slide_views", "messages",
                "threads", "submissions", "students",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM jobs WHERE kind IN ('grading', 'export', 'delete_temp')")
            conn.execute(
                "DELETE FROM jobs WHERE kind = 'mail' AND json_extract(payload_json, '$.category') "
                "IN ('message', 'message_digest')"
            )
            conn.execute(
                "UPDATE maintenance SET deletion_step = 'db_deleted' WHERE id = 1"
            )
    finally:
        conn.close()


def _delete_old_student_files(settings: Settings) -> None:
    for name in ("uploads", "tmp"):
        path = settings.data_dir / name
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
    for folder, pattern in (
        (settings.data_dir / "backups", "hwstud-*.db"),
        (settings.data_dir / "archives", "site-*.tar.gz"),
    ):
        for path in folder.glob(pattern):
            if "-cleanup" not in path.name:
                path.unlink(missing_ok=True)
    conn = connect(settings)
    try:
        conn.execute("UPDATE settings SET value = '' WHERE key = 'latest_works_archive'")
    finally:
        conn.close()
