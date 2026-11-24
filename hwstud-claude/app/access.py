from __future__ import annotations

import sqlite3
from datetime import datetime

from .auth import Actor
from .db import parse_utc


def help_view(actor: Actor, requested: str | None) -> str:
    if actor.is_admin:
        return requested if requested in {"teacher", "student"} else "teacher"
    return "student"


def can_view_help(actor: Actor, requested: str | None) -> bool:
    if requested == "teacher" and not actor.is_admin:
        return False
    return requested in {None, "student", "teacher"}


def exam_active(
    conn: sqlite3.Connection, group_id: int, now: datetime
) -> bool:
    exam = conn.execute("SELECT * FROM exam WHERE id = 1").fetchone()
    if not exam or not exam["enabled"]:
        return False
    start = parse_utc(exam["start_at"])
    end = parse_utc(exam["end_at"])
    if start and now < start:
        return False
    if end and now >= end:
        return False
    groups = conn.execute("SELECT group_id FROM exam_groups").fetchall()
    return not groups or any(row["group_id"] == group_id for row in groups)


def content_visible(
    conn: sqlite3.Connection, group_id: int, now: datetime
) -> bool:
    return not exam_active(conn, group_id, now)


def lecture_visible(
    conn: sqlite3.Connection,
    student: Actor,
    lecture: sqlite3.Row,
    now: datetime,
) -> bool:
    if not student.student_id or not student.group_id:
        return False
    if not content_visible(conn, student.group_id, now):
        return False
    if lecture["status"] != "open":
        return False
    groups = conn.execute(
        "SELECT group_id FROM lecture_groups WHERE lecture_id = ?",
        (lecture["id"],),
    ).fetchall()
    return not groups or any(row["group_id"] == student.group_id for row in groups)
