from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse, Response

from ..auth import Actor, check_csrf, create_student, flash
from ..db import transaction, utc_now_iso
from ..services import personal_keys
from ..services import students as student_service
from ..services.access_cards import access_card_text
from ..web import render
from .admin import require_admin

router = APIRouter(prefix="/admin")


def _local_utc(value: str, settings) -> str | None:
    if not value.strip():
        return None
    local = datetime.fromisoformat(value).replace(tzinfo=settings.timezone)
    return local.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@router.get("/exam")
def exam_page(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        exam = conn.execute("SELECT * FROM exam WHERE id = 1").fetchone()
        groups = conn.execute("SELECT * FROM groups ORDER BY id").fetchall()
        selected = [row["group_id"] for row in conn.execute("SELECT * FROM exam_groups")]
    finally:
        conn.close()
    return render(
        request,
        "admin_exam.html",
        {"title": "Режим экзамена", "exam": exam, "groups": groups, "selected": selected},
        actor=actor,
    )


@router.post("/exam")
def save_exam(
    request: Request,
    enabled: str = Form(""),
    start_at: str = Form(""),
    end_at: str = Form(""),
    selected_groups: list[int] = Form([]),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    try:
        start = _local_utc(start_at, request.app.state.settings)
        end = _local_utc(end_at, request.app.state.settings)
        if start and end and end <= start:
            raise ValueError("Окончание экзамена должно быть позже начала")
        conn = request.app.state.db_connect()
        try:
            with transaction(conn, immediate=True):
                valid = {row["id"] for row in conn.execute("SELECT id FROM groups")}
                if any(group not in valid for group in selected_groups):
                    raise ValueError("Неизвестная группа")
                conn.execute(
                    "UPDATE exam SET enabled = ?, start_at = ?, end_at = ? WHERE id = 1",
                    (int(enabled == "1"), start, end),
                )
                conn.execute("DELETE FROM exam_groups")
                conn.executemany(
                    "INSERT INTO exam_groups(group_id) VALUES (?)",
                    [(group,) for group in selected_groups],
                )
                conn.execute(
                    "INSERT INTO audit_log(at, actor, action, object, details_json) "
                    "VALUES (?, 'admin', 'exam', 'exam', ?)",
                    (utc_now_iso(), f'{{"enabled": {int(enabled == "1")}}}'),
                )
        finally:
            conn.close()
        flash(request, "success", "Настройки экзамена сохранены")
    except ValueError as exc:
        flash(request, "error", str(exc))
    return RedirectResponse("/admin/exam", status_code=303)


@router.get("/about")
def about_editor(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'about_text'").fetchone()
    finally:
        conn.close()
    return render(
        request,
        "admin_about.html",
        {"title": "О курсе и авторе", "about_text": row["value"] if row else ""},
        actor=actor,
    )


@router.post("/about")
def save_about(
    request: Request,
    about_text: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'about_text'", (about_text.strip(),)
        )
    finally:
        conn.close()
    flash(request, "success", "Текст страницы сохранён")
    return RedirectResponse("/admin/about", status_code=303)


@router.post("/settings/distribution")
def distribution_setting(
    request: Request,
    enabled: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'distribution_enabled'",
            (str(int(enabled == "1")),),
        )
    finally:
        conn.close()
    flash(request, "success", "Настройка кривой распределения сохранена")
    return RedirectResponse("/admin/settings", status_code=303)


@router.get("/groups")
def groups_page(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        groups = conn.execute(
            "SELECT g.*, (SELECT COUNT(*) FROM students s WHERE s.group_id = g.id) "
            "AS student_count FROM groups g ORDER BY g.id"
        ).fetchall()
        students = conn.execute(
            "SELECT s.*, g.name AS group_name FROM students s JOIN groups g ON g.id = s.group_id "
            "ORDER BY g.id, s.nick"
        ).fetchall()
    finally:
        conn.close()
    return render(
        request,
        "admin_groups.html",
        {"title": "Группы и студенты", "groups": groups, "students": students},
        actor=actor,
    )


def _admin_access_card(request: Request, student, key: str) -> str:
    return request.app.state.access_cards.create(
        nick=student["nick"], key=key, audience="admin",
        next_path="/admin/groups",
    )


@router.post("/students/create")
def admin_create_student(
    request: Request,
    nick: str = Form(""),
    group_id: int = Form(0),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        student, error = create_student(conn, nick, group_id)
        if error:
            flash(request, "error", error)
            return RedirectResponse("/admin/groups", status_code=303)
        key = personal_keys.issue_key(conn, student["id"], "admin")
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, student_id, details_json) "
            "VALUES (?, 'admin', 'student_created_by_admin', ?, ?)",
            (utc_now_iso(), student["id"], f'{{"group_id": {group_id}}}'),
        )
        card_id = _admin_access_card(request, student, key)
    finally:
        conn.close()
    return RedirectResponse(f"/card/{card_id}", status_code=303)


@router.post("/students/{student_id}/reset-key")
def admin_reset_key(
    request: Request,
    student_id: int,
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        student = conn.execute(
            "SELECT * FROM students WHERE id = ?", (student_id,)
        ).fetchone()
        if not student:
            raise HTTPException(status_code=404)
        key = personal_keys.issue_key(conn, student_id, "admin")
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, student_id, details_json) "
            "VALUES (?, 'admin', 'key_reset', ?, '{}')",
            (utc_now_iso(), student_id),
        )
        card_id = _admin_access_card(request, student, key)
    finally:
        conn.close()
    return RedirectResponse(f"/card/{card_id}", status_code=303)


@router.post("/students/issue-keys")
def admin_issue_missing_keys(
    request: Request,
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    cards = []
    conn = request.app.state.db_connect()
    try:
        students = conn.execute(
            "SELECT * FROM students WHERE key_hash IS NULL ORDER BY id"
        ).fetchall()
        for student in students:
            key = personal_keys.issue_key(conn, student["id"], "transition")
            conn.execute(
                "INSERT INTO audit_log(at, actor, action, student_id, details_json) "
                "VALUES (?, 'admin', 'key_issued_transition', ?, '{}')",
                (utc_now_iso(), student["id"]),
            )
            cards.append({
                "nick": student["nick"],
                "text": access_card_text(
                    request.app.state.settings.app_base_url, student["nick"], key
                ),
            })
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, details_json) "
            "VALUES (?, 'admin', 'keys_bulk_issued', ?)",
            (utc_now_iso(), f'{{"count": {len(cards)}}}'),
        )
    finally:
        conn.close()
    response = render(
        request,
        "access_cards_bulk.html",
        {
            "title": "Ключи для существующих ников", "cards": cards,
            "errors": [], "all_text": "\n\n".join(
                card["text"] for card in cards
            ),
            "back_href": "/admin/groups", "back_label": "К группам",
        },
        actor=actor,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/students/bulk")
def admin_bulk_create(
    request: Request,
    group_id: int = Form(0),
    nicks: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    requested = [line.strip() for line in nicks.splitlines() if line.strip()]
    if not requested:
        flash(request, "error", "Введите хотя бы один ник")
        return RedirectResponse("/admin/groups", status_code=303)
    cards = []
    errors = []
    conn = request.app.state.db_connect()
    try:
        for nick in requested:
            student, error = create_student(conn, nick, group_id)
            if error:
                errors.append({"nick": nick, "error": error})
                continue
            key = personal_keys.issue_key(conn, student["id"], "admin")
            conn.execute(
                "INSERT INTO audit_log(at, actor, action, student_id, details_json) "
                "VALUES (?, 'admin', 'student_created_by_admin', ?, ?)",
                (utc_now_iso(), student["id"], f'{{"group_id": {group_id}}}'),
            )
            cards.append({
                "nick": student["nick"],
                "text": access_card_text(
                    request.app.state.settings.app_base_url, student["nick"], key
                ),
            })
    finally:
        conn.close()
    all_text = "\n\n".join(card["text"] for card in cards)
    response = render(
        request,
        "access_cards_bulk.html",
        {
            "title": "Карточки доступа", "cards": cards, "errors": errors,
            "all_text": all_text, "back_href": "/admin/groups",
            "back_label": "К группам",
        },
        actor=actor,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/students/bulk/download")
def admin_bulk_download(
    request: Request,
    content: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    if not content.strip():
        raise HTTPException(status_code=400)
    return Response(
        content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="hwstud-access-cards.txt"',
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/groups/{group_id}")
def edit_group(
    request: Request,
    group_id: int,
    name: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    if name.strip():
        conn = request.app.state.db_connect()
        try:
            conn.execute(
                "UPDATE groups SET name = ? WHERE id = ?",
                (name.strip(), group_id),
            )
        finally:
            conn.close()
        flash(request, "success", "Группа обновлена")
    return RedirectResponse("/admin/groups", status_code=303)


@router.get("/students/{student_id}/delete")
def delete_student_page(request: Request, student_id: int, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        student = conn.execute(
            "SELECT s.*, g.name AS group_name FROM students s JOIN groups g ON g.id = s.group_id "
            "WHERE s.id = ?",
            (student_id,),
        ).fetchone()
        if not student:
            raise HTTPException(status_code=404)
        works = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE student_id = ?", (student_id,)
        ).fetchone()[0]
        messages = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE student_id = ?", (student_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    return render(
        request,
        "admin_student_delete.html",
        {
            "title": "Удаление ника",
            "student": student,
            "works": works,
            "messages": messages,
        },
        actor=actor,
    )


@router.post("/students/{student_id}/delete")
def delete_student_confirm(
    request: Request,
    student_id: int,
    confirm_delete: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    if confirm_delete != "1":
        flash(request, "error", "Удаление отменено: подтверждение не отмечено")
        return RedirectResponse(f"/admin/students/{student_id}/delete", status_code=303)
    try:
        nick = student_service.delete_student(request.app.state.settings, student_id)
        flash(request, "success", f"Ник {nick} удалён. Место в группе и ник освобождены")
    except ValueError as exc:
        flash(request, "error", str(exc))
    return RedirectResponse("/admin/groups", status_code=303)


@router.post("/students/{student_id}/block")
def toggle_student(
    request: Request,
    student_id: int,
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        conn.execute(
            "UPDATE students SET blocked = 1 - blocked, login_token = ? WHERE id = ?",
            (secrets.token_hex(16), student_id),
        )
    finally:
        conn.close()
    flash(request, "success", "Статус ника изменён; прежние сессии отозваны")
    return RedirectResponse("/admin/groups", status_code=303)
