from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from ..auth import Actor, check_csrf, flash
from ..access import content_visible, lecture_visible
from ..db import transaction, utc_now, utc_now_iso
from ..services import communication
from ..services import assignments as assignment_service
from ..web import render
from .admin import require_admin
from .student import require_user

router = APIRouter()


def _with_links(conn, actor, rows):
    return [
        {
            "announcement": row,
            "link": communication.resolve_announcement_link(conn, actor, row["link"]),
            "read": bool(
                actor.student_id
                and conn.execute(
                    "SELECT 1 FROM announcement_reads WHERE announcement_id = ? AND student_id = ?",
                    (row["id"], actor.student_id),
                ).fetchone()
            ),
        }
        for row in rows
    ]


@router.get("/announcements")
def announcements_page(request: Request, actor: Actor = Depends(require_user)):
    conn = request.app.state.db_connect()
    try:
        rows = communication.visible_announcements(conn, actor)
        items = _with_links(conn, actor, rows)
    finally:
        conn.close()
    return render(
        request,
        "announcements.html",
        {"title": "Объявления", "items": items},
        actor=actor,
    )


@router.get("/announcements/{announcement_id}")
def announcement_detail(
    request: Request, announcement_id: int, actor: Actor = Depends(require_user)
):
    conn = request.app.state.db_connect()
    try:
        row = communication.announcement_by_id(conn, announcement_id, actor)
        if not row:
            raise HTTPException(status_code=404)
        if actor.student_id:
            communication.mark_announcement_read(conn, announcement_id, actor.student_id)
        link = communication.resolve_announcement_link(conn, actor, row["link"])
    finally:
        conn.close()
    return render(
        request,
        "announcement_detail.html",
        {"title": row["title"], "announcement": row, "link": link},
        actor=actor,
    )


def _message_status(conn, row) -> str:
    if row["author"] == "teacher":
        return "Ответ преподавателя"
    answered = conn.execute(
        "SELECT 1 FROM messages WHERE student_id = ? AND author = 'teacher' "
        "AND created_at >= ? LIMIT 1",
        (row["student_id"], row["created_at"]),
    ).fetchone()
    if answered:
        return "Есть ответ"
    return "Прочитано преподавателем" if row["teacher_read_at"] else "Отправлено"


@router.get("/messages")
def messages_page(request: Request, actor: Actor = Depends(require_user)):
    if actor.is_admin:
        return RedirectResponse("/admin/messages", status_code=303)
    conn = request.app.state.db_connect()
    try:
        rows = conn.execute(
            "SELECT * FROM messages WHERE student_id = ? ORDER BY created_at, id",
            (actor.student_id,),
        ).fetchall()
        thread = conn.execute(
            "SELECT * FROM threads WHERE student_id = ?", (actor.student_id,)
        ).fetchone()
        lectures = conn.execute(
            "SELECT id, title FROM lectures WHERE status = 'open' AND ("
            "NOT EXISTS (SELECT 1 FROM lecture_groups lg WHERE lg.lecture_id = lectures.id) "
            "OR EXISTS (SELECT 1 FROM lecture_groups lg WHERE lg.lecture_id = lectures.id "
            "AND lg.group_id = ?)) ORDER BY num, id",
            (actor.group_id,),
        ).fetchall() if content_visible(conn, actor.group_id, utc_now()) else []
        assignments = assignment_service.active_assignments(conn)
        items = [
            {"message": row, "status": _message_status(conn, row), "topic": communication.message_topic(conn, row)}
            for row in rows
        ]
        remaining_messages = max(
            0, 10 - communication.student_messages_today(conn, actor.student_id, request.app.state.settings)
        )
        conn.execute(
            "UPDATE messages SET student_read_at = ? WHERE student_id = ? "
            "AND author = 'teacher' AND student_read_at IS NULL",
            (utc_now_iso(), actor.student_id),
        )
    finally:
        conn.close()
    return render(
        request,
        "messages.html",
        {
            "title": "Связь с преподавателем",
            "items": items,
            "thread": thread,
            "lectures": lectures,
            "assignments": assignments,
            "remaining_messages": remaining_messages,
        },
        actor=actor,
    )


@router.post("/messages")
async def send_message(
    request: Request,
    body: str = Form(""),
    ref_lecture: int | None = Form(None),
    ref_slide: int | None = Form(None),
    ref_assignment: int | None = Form(None),
    csrf: str = Form(""),
    actor: Actor = Depends(require_user),
):
    if actor.is_admin:
        raise HTTPException(status_code=404)
    form = await request.form()
    check_csrf(request, str(form.get("csrf", csrf)))
    body = body.strip()
    settings = request.app.state.settings
    conn = request.app.state.db_connect()
    try:
        if not body or len(body) > 2000:
            raise ValueError("Сообщение должно содержать от 1 до 2000 знаков")
        if communication.student_messages_today(conn, actor.student_id, settings) >= 10:
            raise ValueError("Дневной лимит — 10 сообщений. Новый вопрос можно отправить завтра")
        if ref_lecture:
            lecture = conn.execute(
                "SELECT * FROM lectures WHERE id = ?", (ref_lecture,)
            ).fetchone()
            if not lecture or not lecture_visible(conn, actor, lecture, utc_now()):
                raise ValueError("Лекция для вопроса недоступна")
        if not ref_lecture:
            ref_slide = None
        if ref_assignment and not assignment_service.active_assignment(conn, ref_assignment):
            raise ValueError("Задание для вопроса не найдено")
        with transaction(conn, immediate=True):
            cursor = conn.execute(
                "INSERT INTO messages(student_id, author, body, ref_lecture, ref_slide, "
                "ref_assignment, created_at, student_read_at) VALUES (?, 'student', ?, ?, ?, ?, ?, ?)",
                (
                    actor.student_id, body, ref_lecture, ref_slide, ref_assignment,
                    utc_now_iso(), utc_now_iso(),
                ),
            )
            message_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO threads(student_id, closed_at) VALUES (?, NULL) "
                "ON CONFLICT(student_id) DO UPDATE SET closed_at = NULL",
                (actor.student_id,),
            )
    except ValueError as exc:
        flash(request, "error", str(exc))
        return RedirectResponse("/messages", status_code=303)
    finally:
        conn.close()
    try:
        communication.queue_message_email(settings, message_id)
    except Exception:
        flash(request, "error", "Сообщение сохранено, но письмо будет отправлено после восстановления очереди")
    else:
        flash(request, "success", "Вопрос отправлен. Ответ появится здесь, на сайте")
    return RedirectResponse("/messages", status_code=303)


def _local_to_utc(value: str, settings) -> str:
    local = datetime.fromisoformat(value).replace(tzinfo=settings.timezone)
    return local.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@router.get("/admin/announcements")
def admin_announcements(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        announcements = conn.execute(
            "SELECT * FROM announcements ORDER BY publish_at DESC, id DESC"
        ).fetchall()
        groups = conn.execute("SELECT * FROM groups ORDER BY id").fetchall()
        lectures = conn.execute("SELECT id, title FROM lectures ORDER BY num, id").fetchall()
        assignments = assignment_service.active_assignments(conn)
        reads = conn.execute(
            "SELECT ar.announcement_id, g.name AS group_name, COUNT(*) AS count "
            "FROM announcement_reads ar JOIN students s ON s.id = ar.student_id "
            "JOIN groups g ON g.id = s.group_id GROUP BY ar.announcement_id, g.id"
        ).fetchall()
    finally:
        conn.close()
    return render(
        request,
        "admin_announcements.html",
        {
            "title": "Объявления",
            "announcements": announcements,
            "groups": groups,
            "lectures": lectures,
            "assignments": assignments,
            "reads": reads,
        },
        actor=actor,
    )


@router.post("/admin/announcements")
def create_announcement(
    request: Request,
    title: str = Form(""),
    body: str = Form(""),
    kind: str = Form("normal"),
    pinned: str = Form(""),
    publish_now: str = Form(""),
    publish_at: str = Form(""),
    unpublish_at: str = Form(""),
    selected_groups: list[int] = Form([]),
    link_type: str = Form("none"),
    lecture_id: int | None = Form(None),
    assignment_id: int | None = Form(None),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    settings = request.app.state.settings
    try:
        if not title.strip() or not body.strip():
            raise ValueError("Заполните заголовок и текст")
        if kind not in {"normal", "important", "deadline"}:
            raise ValueError("Неизвестный вид объявления")
        published = utc_now_iso() if publish_now == "1" else _local_to_utc(publish_at, settings)
        unpublished = _local_to_utc(unpublish_at, settings) if unpublish_at.strip() else None
        if unpublished and unpublished <= published:
            raise ValueError("Дата снятия должна быть позже публикации")
        if link_type == "lecture":
            if not lecture_id:
                raise ValueError("Выберите лекцию")
            conn = request.app.state.db_connect()
            try:
                lecture = conn.execute("SELECT slug FROM lectures WHERE id = ?", (lecture_id,)).fetchone()
            finally:
                conn.close()
            if not lecture:
                raise ValueError("Лекция не найдена")
            link = f"/lectures/{lecture['slug']}"
        elif link_type == "assignment":
            if not assignment_id:
                raise ValueError("Выберите задание")
            link = "/assignments"
        else:
            link = None
        conn = request.app.state.db_connect()
        try:
            with transaction(conn, immediate=True):
                valid_groups = {row["id"] for row in conn.execute("SELECT id FROM groups")}
                if any(group not in valid_groups for group in selected_groups):
                    raise ValueError("Неизвестная группа")
                cursor = conn.execute(
                    "INSERT INTO announcements(title, body, link, kind, pinned, publish_at, unpublish_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        title.strip(), body.strip(), link, kind,
                        int(kind == "important" or pinned == "1"), published, unpublished,
                    ),
                )
                announcement_id = int(cursor.lastrowid)
                conn.executemany(
                    "INSERT INTO announcement_groups(announcement_id, group_id) VALUES (?, ?)",
                    [(announcement_id, group) for group in selected_groups],
                )
        finally:
            conn.close()
        flash(request, "success", "Объявление опубликовано или поставлено по расписанию")
    except (ValueError, TypeError) as exc:
        flash(request, "error", str(exc))
    return RedirectResponse("/admin/announcements", status_code=303)


@router.get("/admin/messages")
def admin_threads(request: Request, q: str = "", actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        threads = conn.execute(
            "SELECT s.id AS student_id, s.nick, g.name AS group_name, t.closed_at, "
            "MAX(m.created_at) AS last_at, "
            "SUM(CASE WHEN m.author = 'student' AND m.teacher_read_at IS NULL THEN 1 ELSE 0 END) AS unread "
            "FROM students s JOIN groups g ON g.id = s.group_id "
            "JOIN messages m ON m.student_id = s.id "
            "LEFT JOIN threads t ON t.student_id = s.id "
            "WHERE (? = '' OR lower(s.nick) LIKE lower(?)) "
            "GROUP BY s.id ORDER BY unread DESC, last_at DESC",
            (q.strip(), f"%{q.strip()}%"),
        ).fetchall()
    finally:
        conn.close()
    return render(
        request,
        "admin_messages.html",
        {"title": "Сообщения", "threads": threads, "q": q},
        actor=actor,
    )


@router.get("/admin/messages/{student_id}")
def admin_thread(request: Request, student_id: int, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        student = conn.execute(
            "SELECT s.*, g.name AS group_name FROM students s JOIN groups g ON g.id = s.group_id "
            "WHERE s.id = ?",
            (student_id,),
        ).fetchone()
        if not student:
            raise HTTPException(status_code=404)
        rows = conn.execute(
            "SELECT * FROM messages WHERE student_id = ? ORDER BY created_at, id",
            (student_id,),
        ).fetchall()
        thread = conn.execute("SELECT * FROM threads WHERE student_id = ?", (student_id,)).fetchone()
        conn.execute(
            "UPDATE messages SET teacher_read_at = ? WHERE student_id = ? "
            "AND author = 'student' AND teacher_read_at IS NULL",
            (utc_now_iso(), student_id),
        )
        items = [
            {"message": row, "topic": communication.message_topic(conn, row)}
            for row in rows
        ]
    finally:
        conn.close()
    return render(
        request,
        "admin_thread.html",
        {
            "title": f"Переписка с {student['nick']}",
            "student": student,
            "items": items,
            "thread": thread,
        },
        actor=actor,
    )


@router.post("/admin/messages/{student_id}/reply")
def admin_reply(
    request: Request,
    student_id: int,
    body: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    body = body.strip()
    if not body or len(body) > 2000:
        flash(request, "error", "Ответ должен содержать от 1 до 2000 знаков")
    else:
        conn = request.app.state.db_connect()
        try:
            conn.execute(
                "INSERT INTO messages(student_id, author, body, created_at, teacher_read_at) "
                "VALUES (?, 'teacher', ?, ?, ?)",
                (student_id, body, utc_now_iso(), utc_now_iso()),
            )
        finally:
            conn.close()
        flash(request, "success", "Ответ опубликован в ветке студента")
    return RedirectResponse(f"/admin/messages/{student_id}", status_code=303)


@router.post("/admin/messages/{student_id}/close")
def close_thread(
    request: Request,
    student_id: int,
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        conn.execute(
            "INSERT INTO threads(student_id, closed_at) VALUES (?, ?) "
            "ON CONFLICT(student_id) DO UPDATE SET closed_at = excluded.closed_at",
            (student_id, utc_now_iso()),
        )
    finally:
        conn.close()
    flash(request, "success", "Вопрос отмечен решённым. Новое сообщение студента снова откроет ветку")
    return RedirectResponse(f"/admin/messages/{student_id}", status_code=303)
