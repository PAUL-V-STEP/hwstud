from __future__ import annotations

import json
from pathlib import Path

from ..config import Settings
from ..db import connect, transaction, utc_now_iso


def _safe_unlink(root: Path, path: Path) -> None:
    resolved = path.resolve()
    if resolved.is_relative_to(root.resolve()) and resolved.is_file():
        resolved.unlink(missing_ok=True)


def delete_student(settings: Settings, student_id: int) -> str:
    conn = connect(settings)
    files: list[Path] = []
    prepared_files: list[Path] = []
    try:
        student = conn.execute(
            "SELECT * FROM students WHERE id = ?", (student_id,)
        ).fetchone()
        if not student:
            raise ValueError("Студент не найден")
        nick = student["nick"]
        submissions = conn.execute(
            "SELECT id, file FROM submissions WHERE student_id = ?", (student_id,)
        ).fetchall()
        submission_ids = [row["id"] for row in submissions]
        files.extend(settings.data_dir / row["file"] for row in submissions)
        if submission_ids:
            placeholders = ",".join("?" for _ in submission_ids)
            jobs = conn.execute(
                f"SELECT payload_json FROM jobs WHERE kind = 'grading' "
                f"AND json_extract(payload_json, '$.submission_id') IN ({placeholders})",
                submission_ids,
            ).fetchall()
            for job in jobs:
                payload = json.loads(job["payload_json"])
                if payload.get("prepared"):
                    prepared_files.append(settings.data_dir / payload["prepared"])

        with transaction(conn, immediate=True):
            conn.execute(
                "UPDATE audit_log SET actor = 'удалённый студент', object = 'удалённый студент', "
                "details_json = 'удалённый студент', student_id = NULL WHERE student_id = ?",
                (student_id,),
            )
            if submission_ids:
                placeholders = ",".join("?" for _ in submission_ids)
                conn.execute(
                    f"DELETE FROM jobs WHERE kind = 'grading' "
                    f"AND json_extract(payload_json, '$.submission_id') IN ({placeholders})",
                    submission_ids,
                )
            conn.execute(
                "DELETE FROM jobs WHERE kind = 'export' AND status IN ('queued', 'running')"
            )
            conn.execute(
                "DELETE FROM jobs WHERE kind = 'mail' AND status IN ('queued', 'running') "
                "AND json_extract(payload_json, '$.category') IN ('message', 'message_digest')"
            )
            conn.execute(
                "DELETE FROM students WHERE id = ?", (student_id,)
            )
            conn.execute(
                "INSERT INTO audit_log(at, actor, action, object) "
                "VALUES (?, 'admin', 'student_deleted', NULL)",
                (utc_now_iso(),),
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES ('last_message_digest_slot', '') "
                "ON CONFLICT(key) DO UPDATE SET value = ''"
            )
            latest = conn.execute(
                "SELECT value FROM settings WHERE key = 'latest_works_archive'"
            ).fetchone()
            latest_archive = latest["value"] if latest else ""
            if latest_archive:
                conn.execute("UPDATE settings SET value = '' WHERE key = 'latest_works_archive'")
    finally:
        conn.close()

    for path in files:
        _safe_unlink(settings.data_dir / "uploads", path)
    for path in prepared_files:
        _safe_unlink(settings.data_dir / "tmp", path)
    if latest_archive:
        from .backup import delete_temporary_file

        delete_temporary_file(settings, latest_archive)
    return nick
