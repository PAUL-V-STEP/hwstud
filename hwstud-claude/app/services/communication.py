from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..access import exam_active, lecture_visible
from ..config import Settings
from ..db import connect, parse_utc, transaction, utc_now, utc_now_iso


def _announcement_sql() -> str:
    return (
        "SELECT * FROM announcements WHERE publish_at <= ? "
        "AND (unpublish_at IS NULL OR unpublish_at > ?)"
    )


def visible_announcements(conn, actor, *, limit: int | None = None):
    now = utc_now_iso()
    if actor.is_admin:
        sql = _announcement_sql() + " ORDER BY pinned DESC, publish_at DESC"
        parameters: list[Any] = [now, now]
    else:
        sql = _announcement_sql() + (
            " AND (NOT EXISTS (SELECT 1 FROM announcement_groups ag "
            "WHERE ag.announcement_id = announcements.id) OR EXISTS "
            "(SELECT 1 FROM announcement_groups ag WHERE ag.announcement_id = announcements.id "
            "AND ag.group_id = ?)) ORDER BY pinned DESC, publish_at DESC"
        )
        parameters = [now, now, actor.group_id]
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, parameters).fetchall()


def unread_announcements_count(conn, actor) -> int:
    rows = visible_announcements(conn, actor)
    return sum(
        1
        for row in rows
        if not conn.execute(
            "SELECT 1 FROM announcement_reads WHERE announcement_id = ? AND student_id = ?",
            (row["id"], actor.student_id),
        ).fetchone()
    )


def new_teacher_replies_count(conn, student_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE student_id = ? AND author = 'teacher' "
        "AND student_read_at IS NULL",
        (student_id,),
    ).fetchone()[0]


def announcement_by_id(conn, announcement_id: int, actor):
    row = conn.execute(
        "SELECT * FROM announcements WHERE id = ?", (announcement_id,)
    ).fetchone()
    if not row:
        return None
    visible = any(item["id"] == announcement_id for item in visible_announcements(conn, actor))
    return row if visible else None


def mark_announcement_read(conn, announcement_id: int, student_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO announcement_reads(announcement_id, student_id, read_at) "
        "VALUES (?, ?, ?)",
        (announcement_id, student_id, utc_now_iso()),
    )


def resolve_announcement_link(conn, actor, link: str | None) -> dict[str, Any] | None:
    if not link:
        return None
    if link == "/assignments":
        return {"href": link, "text": "Перейти к заданиям", "disabled": False}
    if link.startswith("/lectures/"):
        slug = link.removeprefix("/lectures/")
        lecture = conn.execute(
            "SELECT * FROM lectures WHERE slug = ?", (slug,)
        ).fetchone()
        if not lecture:
            return {"href": None, "text": "Материал недоступен", "disabled": True}
        if actor.is_admin:
            return {"href": link, "text": lecture["title"], "disabled": False}
        if exam_active(conn, actor.group_id, utc_now()):
            return {
                "href": None,
                "text": lecture["title"],
                "disabled": True,
                "reason": "закрыто на время экзамена",
            }
        if not lecture_visible(conn, actor, lecture, utc_now()):
            return {
                "href": None,
                "text": lecture["title"],
                "disabled": True,
                "reason": "пока недоступна",
            }
        return {"href": link, "text": lecture["title"], "disabled": False}
    return None


def message_topic(conn, message) -> str:
    if message["ref_assignment"]:
        row = conn.execute(
            "SELECT title FROM assignments WHERE id = ?", (message["ref_assignment"],)
        ).fetchone()
        return f"Задание: {row['title']}" if row else "Задание"
    if message["ref_lecture"]:
        row = conn.execute(
            "SELECT title FROM lectures WHERE id = ?", (message["ref_lecture"],)
        ).fetchone()
        title = row["title"] if row else "Лекция"
        return f"{title}, слайд {message['ref_slide']}" if message["ref_slide"] else title
    return "Общий вопрос"


def student_messages_today(conn, student_id: int, settings: Settings) -> int:
    now_local = utc_now().astimezone(settings.timezone)
    start_local = datetime(
        now_local.year, now_local.month, now_local.day, tzinfo=settings.timezone
    )
    start_utc = start_local.astimezone(UTC).isoformat().replace(
        "+00:00", "Z"
    )
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE student_id = ? AND author = 'student' "
        "AND created_at >= ?",
        (student_id, start_utc),
    ).fetchone()[0]


def queue_message_email(settings: Settings, message_id: int) -> str:
    conn = connect(settings)
    try:
        message = conn.execute(
            "SELECT m.*, s.nick, g.name AS group_name FROM messages m "
            "JOIN students s ON s.id = m.student_id JOIN groups g ON g.id = s.group_id "
            "WHERE m.id = ?",
            (message_id,),
        ).fetchone()
        if not message or message["author"] != "student":
            return "not_applicable"
        mode_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'mail_mode'"
        ).fetchone()
        if mode_row and mode_row["value"] == "hourly":
            return "hourly"
        now_local = utc_now().astimezone(settings.timezone)
        start_local = datetime(
            now_local.year, now_local.month, now_local.day, tzinfo=settings.timezone
        )
        start_utc = start_local.astimezone(UTC).isoformat().replace(
            "+00:00", "Z"
        )
        sent = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE author = 'student' AND mailed_at IS NOT NULL "
            "AND mailed_at >= ?",
            (start_utc,),
        ).fetchone()[0]
        if sent >= 30:
            return "hourly"
        topic = message_topic(conn, message)
        body = (
            f"Группа: {message['group_name']}\nНик: {message['nick']}\n"
            f"Тема: {topic}\n\n{message['body']}\n\n"
            f"Ветка: {settings.app_base_url}/admin/messages/{message['student_id']}"
        )
        payload = {
            "category": "message",
            "message_id": message_id,
            "subject": f"hwstud: вопрос от {message['nick']}, {message['group_name']}",
            "body": body,
        }
        with transaction(conn):
            conn.execute(
                "INSERT INTO jobs(kind, priority, payload_json, run_after) "
                "VALUES ('mail', 1, ?, ?)",
                (json.dumps(payload, ensure_ascii=False), utc_now_iso()),
            )
        return "immediate"
    finally:
        conn.close()


def enqueue_hourly_digest_if_due(settings: Settings, now: datetime | None = None) -> bool:
    now = now or utc_now()
    local = now.astimezone(settings.timezone)
    if local.minute != 0:
        return False
    slot = local.strftime("%Y-%m-%dT%H:00")
    conn = connect(settings)
    try:
        with transaction(conn, immediate=True):
            previous = conn.execute(
                "SELECT value FROM settings WHERE key = 'last_message_digest_slot'"
            ).fetchone()
            if previous and previous["value"] == slot:
                return False
            conn.execute(
                "INSERT INTO settings(key, value) VALUES ('last_message_digest_slot', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (slot,),
            )
            messages = conn.execute(
                "SELECT m.*, s.nick, g.name AS group_name FROM messages m "
                "JOIN students s ON s.id = m.student_id JOIN groups g ON g.id = s.group_id "
                "WHERE m.author = 'student' AND m.mailed_at IS NULL ORDER BY m.created_at"
            ).fetchall()
            if not messages:
                return False
            pending = conn.execute(
                "SELECT 1 FROM jobs WHERE kind = 'mail' AND status IN ('queued', 'running') "
                "AND json_extract(payload_json, '$.category') = 'message_digest'"
            ).fetchone()
            if pending:
                return False
            lines = []
            for message in messages:
                topic = message_topic(conn, message)
                lines.append(
                    f"— {message['group_name']} / {message['nick']} / {topic}\n"
                    f"{message['body']}\n{settings.app_base_url}/admin/messages/{message['student_id']}"
                )
            payload = {
                "category": "message_digest",
                "message_ids": [message["id"] for message in messages],
                "subject": f"hwstud: сводка сообщений ({len(messages)})",
                "body": "\n\n".join(lines),
            }
            conn.execute(
                "INSERT INTO jobs(kind, priority, payload_json, run_after) "
                "VALUES ('mail', 1, ?, ?)",
                (json.dumps(payload, ensure_ascii=False), utc_now_iso()),
            )
            return True
    finally:
        conn.close()


def mark_mailed(settings: Settings, payload: dict[str, Any]) -> None:
    ids = []
    if payload.get("message_id"):
        ids = [int(payload["message_id"])]
    elif payload.get("message_ids"):
        ids = [int(value) for value in payload["message_ids"]]
    if not ids:
        return
    conn = connect(settings)
    try:
        with transaction(conn):
            conn.executemany(
                "UPDATE messages SET mailed_at = ? WHERE id = ?",
                [(utc_now_iso(), message_id) for message_id in ids],
            )
    finally:
        conn.close()
