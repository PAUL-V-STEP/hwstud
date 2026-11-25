from __future__ import annotations

import json
from datetime import datetime, timedelta

from ..db import parse_utc, transaction, utc_now, utc_now_iso

DEFAULT_WINDOW_MINUTES = 10
MIN_WINDOW_MINUTES = 5
MAX_WINDOW_MINUTES = 15
MAX_TOTAL_WINDOW_MINUTES = 60


def _moment(value: datetime | None) -> datetime:
    return value or utc_now()


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def window_minutes(conn) -> int:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'registration_window_minutes'"
    ).fetchone()
    minutes = int(row["value"]) if row else DEFAULT_WINDOW_MINUTES
    if not MIN_WINDOW_MINUTES <= minutes <= MAX_WINDOW_MINUTES:
        raise ValueError("Длительность окна регистрации должна быть от 5 до 15 минут")
    return minutes


def group_registration_error(group, *, now: datetime | None = None) -> str | None:
    until = parse_utc(group["enroll_until"])
    if not until:
        return (
            "Регистрация сейчас закрыта. Её открывает преподаватель на занятии. "
            "Если вы пропустили занятие, напишите преподавателю — он создаст ник и пришлёт ключ"
        )
    if until <= _moment(now):
        return "Время регистрации истекло. Попросите преподавателя продлить регистрацию"
    return None


def group_is_open(conn, group_id: int, *, now: datetime | None = None) -> bool:
    row = conn.execute(
        "SELECT enroll_until FROM groups WHERE id = ?", (group_id,)
    ).fetchone()
    return bool(row and group_registration_error(row, now=now) is None)


def open_groups(conn, *, now: datetime | None = None):
    rows = conn.execute(
        "SELECT * FROM groups WHERE enroll_until IS NOT NULL ORDER BY id"
    ).fetchall()
    return [row for row in rows if group_is_open(conn, row["id"], now=now)]


def open_window(conn, group_ids: list[int], *, now: datetime | None = None) -> int:
    ids = list(dict.fromkeys(int(group_id) for group_id in group_ids))
    if not ids:
        raise ValueError("Выберите хотя бы одну группу")
    started = _moment(now)
    minutes = window_minutes(conn)
    until = started + timedelta(minutes=minutes)
    with transaction(conn, immediate=True):
        placeholders = ",".join("?" for _ in ids)
        found = conn.execute(
            f"SELECT COUNT(*) FROM groups WHERE id IN ({placeholders})", ids
        ).fetchone()[0]
        if found != len(ids):
            raise ValueError("Выбранная группа не найдена")
        conn.execute(
            f"UPDATE groups SET enroll_until = ?, enroll_window_started = ?, "
            f"enroll_open = 0 WHERE id IN ({placeholders})",
            [_iso(until), _iso(started), *ids],
        )
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, details_json) "
            "VALUES (?, 'admin', 'registration_opened', ?)",
            (
                utc_now_iso(),
                json.dumps({"group_ids": ids, "minutes": minutes}, ensure_ascii=False),
            ),
        )
    return minutes


def extend_window(conn, group_id: int, *, now: datetime | None = None) -> str:
    current = _moment(now)
    minutes = window_minutes(conn)
    with transaction(conn, immediate=True):
        group = conn.execute(
            "SELECT * FROM groups WHERE id = ?", (group_id,)
        ).fetchone()
        if not group:
            raise ValueError("Группа не найдена")
        until = parse_utc(group["enroll_until"])
        started = parse_utc(group["enroll_window_started"])
        if not until or until <= current or not started:
            raise ValueError("Сначала откройте регистрацию для этой группы")
        extended = until + timedelta(minutes=minutes)
        latest = started + timedelta(minutes=MAX_TOTAL_WINDOW_MINUTES)
        if extended > latest:
            raise ValueError("Общая длительность окна не может превышать 60 минут")
        conn.execute(
            "UPDATE groups SET enroll_until = ? WHERE id = ?",
            (_iso(extended), group_id),
        )
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, details_json) "
            "VALUES (?, 'admin', 'registration_extended', ?)",
            (
                utc_now_iso(),
                json.dumps({"group_id": group_id, "until": _iso(extended)}, ensure_ascii=False),
            ),
        )
        return _iso(extended)


def close_windows(
    conn, group_id: int | None = None, *, close_all: bool = False
) -> list[int]:
    with transaction(conn, immediate=True):
        if close_all:
            rows = conn.execute(
                "SELECT id FROM groups WHERE enroll_until IS NOT NULL"
            ).fetchall()
            ids = [row["id"] for row in rows]
            conn.execute(
                "UPDATE groups SET enroll_until = NULL, enroll_window_started = NULL"
            )
        else:
            group = conn.execute(
                "SELECT id FROM groups WHERE id = ?", (group_id,)
            ).fetchone()
            if not group:
                raise ValueError("Группа не найдена")
            ids = [group["id"]]
            conn.execute(
                "UPDATE groups SET enroll_until = NULL, enroll_window_started = NULL "
                "WHERE id = ?",
                (group["id"],),
            )
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, details_json) "
            "VALUES (?, 'admin', 'registration_closed', ?)",
            (utc_now_iso(), json.dumps({"group_ids": ids}, ensure_ascii=False)),
        )
        return ids


def status(conn, *, now: datetime | None = None) -> dict:
    current = _moment(now)
    windows = []
    new_students = []
    for group in open_groups(conn, now=current):
        until = parse_utc(group["enroll_until"])
        windows.append({
            "group_id": group["id"],
            "group_name": group["name"],
            "seconds_left": max(0, int((until - current).total_seconds())),
        })
        rows = conn.execute(
            "SELECT id, nick, created_at FROM students "
            "WHERE group_id = ? AND created_at >= ? ORDER BY created_at, id",
            (group["id"], group["enroll_window_started"]),
        ).fetchall()
        for row in rows:
            new_students.append({
                "id": row["id"],
                "nick": row["nick"],
                "group_name": group["name"],
                "created_at": row["created_at"],
            })
    new_students.sort(key=lambda item: (item["created_at"], item["id"]))
    return {"windows": windows, "new_students": new_students}
