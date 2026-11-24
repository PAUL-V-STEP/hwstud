from __future__ import annotations

from collections import Counter
from typing import Any

from ..access import content_visible, exam_active
from ..config import Settings
from ..db import parse_utc, utc_now, utc_now_iso
from . import assignments as assignment_service

DECIDED = ("accepted", "revise", "exhausted")


def swot(conn, actor, settings: Settings) -> dict[str, list[dict[str, str]]]:
    result = {"S": [], "W": [], "O": [], "T": []}
    submissions = conn.execute(
        "SELECT s.*, a.title, a.due_at FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
        "WHERE s.student_id = ? AND s.id IN (SELECT MAX(id) FROM submissions "
        "WHERE student_id = ? GROUP BY assignment_id)",
        (actor.student_id, actor.student_id),
    ).fetchall()
    assignments = assignment_service.active_assignments(conn)
    active_ids = {assignment["id"] for assignment in assignments}
    submissions = [row for row in submissions if row["assignment_id"] in active_ids]
    now = utc_now()
    for row in submissions:
        link = f"/assignments#assignment-{row['assignment_id']}"
        if row["status"] == "accepted" and row["score"] >= 6:
            label = "высокий балл" if row["score"] >= 8 else "принято"
            result["S"].append({"text": f"{row['title']}: {row['score']} / 10, {label}", "href": link})
        if row["status"] in {"revise", "exhausted"} or (
            row["status"] == "accepted" and row["score"] <= 5
        ):
            result["W"].append({"text": f"{row['title']}: {row['status']}", "href": link})
        due = parse_utc(row["due_at"])
        if due and now <= due and (due - now).total_seconds() <= 7 * 86400 and row["status"] not in {"accepted", "exhausted"}:
            result["T"].append({"text": f"Срок близко: {row['title']}", "href": link})
        if row["status"] == "revise" and row["attempt"] == 3:
            result["T"].append({"text": f"{row['title']}: осталась одна попытка", "href": link})

    for assignment in assignments:
        latest = next((row for row in submissions if row["assignment_id"] == assignment["id"]), None)
        if latest:
            continue
        due = parse_utc(assignment["due_at"])
        if due and (due - now).total_seconds() > 7 * 86400:
            result["O"].append(
                {"text": f"Возможность начать: {assignment['title']}", "href": "/assignments"}
            )
        if due and (due - now).total_seconds() <= 7 * 86400:
            result["T"].append(
                {"text": f"Не сдано, срок близко: {assignment['title']}", "href": "/assignments"}
            )

    if content_visible(conn, actor.group_id, now):
        lectures = conn.execute(
            "SELECT * FROM lectures WHERE status = 'open' AND ("
            "NOT EXISTS (SELECT 1 FROM lecture_groups lg WHERE lg.lecture_id = lectures.id) "
            "OR EXISTS (SELECT 1 FROM lecture_groups lg WHERE lg.lecture_id = lectures.id AND lg.group_id = ?)) "
            "ORDER BY num, id",
            (actor.group_id,),
        ).fetchall()
        for lecture in lectures:
            total = conn.execute(
                "SELECT COUNT(*) FROM slides WHERE lecture_id = ?", (lecture["id"],)
            ).fetchone()[0]
            viewed = conn.execute(
                "SELECT COUNT(*) FROM slide_views WHERE student_id = ? AND lecture_id = ?",
                (actor.student_id, lecture["id"]),
            ).fetchone()[0]
            if total and viewed * 2 < total:
                result["O"].append(
                    {"text": f"Продолжить чтение: {lecture['title']}", "href": f"/lectures/{lecture['slug']}"}
                )
    exam = conn.execute("SELECT * FROM exam WHERE id = 1").fetchone()
    if exam and exam["enabled"]:
        result["T"].append({"text": "Включён или назначен режим экзамена", "href": "/help"})
    return result


def decided_scores(conn, actor, variant: str) -> tuple[list[int], int | None]:
    active_ids = [
        row["id"] for row in assignment_service.active_assignments(conn)
    ]
    if not active_ids:
        return [], None
    placeholders = ",".join("?" for _ in DECIDED)
    if variant == "sum":
        assignment_placeholders = ",".join("?" for _ in active_ids)
        rows = conn.execute(
            f"SELECT student_id, SUM(score) AS total, COUNT(DISTINCT assignment_id) AS count "
            f"FROM submissions WHERE status IN ({placeholders}) AND score IS NOT NULL "
            f"AND assignment_id IN ({assignment_placeholders}) "
            f"AND id IN (SELECT MAX(id) FROM submissions WHERE status IN ({placeholders}) "
            f"AND assignment_id IN ({assignment_placeholders}) "
            f"GROUP BY student_id, assignment_id) "
            f"GROUP BY student_id HAVING count = {len(active_ids)}",
            (*DECIDED, *active_ids, *DECIDED, *active_ids),
        ).fetchall()
        rows = [
            row for row in rows
            if conn.execute("SELECT group_id FROM students WHERE id = ?", (row["student_id"],)).fetchone()[0]
            == actor.group_id
        ]
        own = next((row["total"] for row in rows if row["student_id"] == actor.student_id), None)
        return [row["total"] for row in rows], own

    assignment_id = int(variant)
    if assignment_id not in active_ids:
        return [], None
    rows = conn.execute(
        f"SELECT s.student_id, s.score FROM submissions s JOIN students st ON st.id = s.student_id "
        f"WHERE s.assignment_id = ? AND st.group_id = ? AND s.status IN ({placeholders}) "
        f"AND s.score IS NOT NULL AND s.id IN ("
        f"SELECT MAX(id) FROM submissions WHERE status IN ({placeholders}) "
        f"GROUP BY student_id, assignment_id)",
        (assignment_id, actor.group_id, *DECIDED, *DECIDED),
    ).fetchall()
    own = next((row["score"] for row in rows if row["student_id"] == actor.student_id), None)
    return [row["score"] for row in rows], own


def distribution_svg(
    scores: list[int], own: int | None, variant: str, assignment_count: int = 5
) -> dict[str, Any] | None:
    if len(scores) < 5:
        return None
    maximum = assignment_count * 10 if variant == "sum" else 10
    counts = Counter(scores)
    peak = max(counts.values())
    points = []
    for score in range(maximum + 1):
        x = 40 + score * 520 / maximum
        y = 175 - counts.get(score, 0) / peak * 135
        points.append(f"{x:.1f},{y:.1f}")
    own_x = None if own is None else 40 + own * 520 / maximum
    return {
        "points": " ".join(points),
        "own_x": own_x,
        "maximum": maximum,
        "students": len(scores),
    }
