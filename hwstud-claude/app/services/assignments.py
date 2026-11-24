from __future__ import annotations

import json

from ..db import transaction, utc_now_iso

MAX_ASSIGNMENTS = 10


def configured_count(conn) -> int:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'assignments_count'"
    ).fetchone()
    if row:
        return max(0, min(MAX_ASSIGNMENTS, int(row["value"])))
    return conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]


def active_assignments(conn):
    return conn.execute(
        "SELECT * FROM assignments WHERE position BETWEEN 1 AND ? ORDER BY position",
        (configured_count(conn),),
    ).fetchall()


def active_assignment(conn, assignment_id: int):
    row = conn.execute(
        "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
    ).fetchone()
    if not row or not 1 <= row["position"] <= configured_count(conn):
        return None
    return row


def set_count(conn, count: int) -> None:
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_ASSIGNMENTS:
        raise ValueError("Число домашних заданий должно быть от 0 до 10")
    with transaction(conn, immediate=True):
        cursor = conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'assignments_count'",
            (str(count),),
        )
        if cursor.rowcount == 0:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES ('assignments_count', ?)",
                (str(count),),
            )
        current_positions = {
            row["position"]
            for row in conn.execute("SELECT position FROM assignments")
        }
        for position in range(1, count + 1):
            if position in current_positions:
                continue
            conn.execute(
                "INSERT INTO assignments(title, position) VALUES (?, ?)",
                (f"Задание {position}", position),
            )
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, object, details_json) "
            "VALUES (?, 'admin', 'assignments_count', 'settings', ?)",
            (utc_now_iso(), json.dumps({"count": count}, ensure_ascii=False)),
        )


def move(conn, assignment_id: int, direction: str) -> bool:
    if direction not in {"up", "down"}:
        raise ValueError("Неизвестное направление перемещения")
    with transaction(conn, immediate=True):
        current = conn.execute(
            "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        count = configured_count(conn)
        if not current or not 1 <= current["position"] <= count:
            raise ValueError("Задание не входит в текущий набор")
        target_position = current["position"] + (-1 if direction == "up" else 1)
        if not 1 <= target_position <= count:
            return False
        neighbour = conn.execute(
            "SELECT * FROM assignments WHERE position = ?", (target_position,)
        ).fetchone()
        if not neighbour:
            raise ValueError("Не удалось определить соседнее задание")
        conn.execute(
            "UPDATE assignments SET position = -1 WHERE id = ?", (assignment_id,)
        )
        conn.execute(
            "UPDATE assignments SET position = ? WHERE id = ?",
            (current["position"], neighbour["id"]),
        )
        conn.execute(
            "UPDATE assignments SET position = ? WHERE id = ?",
            (target_position, assignment_id),
        )
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, object, details_json) "
            "VALUES (?, 'admin', 'assignment_moved', ?, ?)",
            (
                utc_now_iso(),
                str(assignment_id),
                json.dumps(
                    {"from": current["position"], "to": target_position},
                    ensure_ascii=False,
                ),
            ),
        )
        return True
