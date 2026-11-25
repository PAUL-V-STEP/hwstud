from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import JSONResponse, RedirectResponse

from ..auth import Actor, check_csrf, flash
from ..db import utc_now_iso
from ..services import registration
from ..services import students as student_service
from ..web import render
from .admin import require_admin

router = APIRouter(prefix="/admin")


@router.get("/registration")
def registration_page(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        groups = conn.execute(
            "SELECT g.*, (SELECT COUNT(*) FROM students s WHERE s.group_id = g.id) "
            "AS student_count FROM groups g ORDER BY g.id"
        ).fetchall()
        state = registration.status(conn)
        minutes = registration.window_minutes(conn)
    finally:
        conn.close()
    return render(
        request,
        "admin_registration.html",
        {
            "title": "Регистрация", "groups": groups, "state": state,
            "minutes": minutes, "back_href": "/admin", "back_label": "Обзор",
        },
        actor=actor,
    )


@router.post("/registration/open")
def registration_open(
    request: Request,
    group_ids: list[int] = Form([]),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        try:
            minutes = registration.open_window(conn, group_ids)
        except ValueError as exc:
            flash(request, "error", str(exc))
        else:
            flash(request, "success", f"Регистрация открыта на {minutes} минут")
    finally:
        conn.close()
    return RedirectResponse("/admin/registration", status_code=303)


@router.post("/registration/extend")
def registration_extend(
    request: Request,
    group_id: int = Form(0),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        try:
            until = registration.extend_window(conn, group_id)
        except ValueError as exc:
            flash(request, "error", str(exc))
        else:
            flash(request, "success", f"Окно продлено до {until}")
    finally:
        conn.close()
    return RedirectResponse("/admin/registration", status_code=303)


@router.post("/registration/close")
def registration_close(
    request: Request,
    group_id: int | None = Form(None),
    close_all: str = Form("", alias="all"),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        try:
            if close_all != "1" and group_id is None:
                raise ValueError("Выберите группу для закрытия")
            registration.close_windows(
                conn, group_id, close_all=close_all == "1"
            )
        except ValueError as exc:
            flash(request, "error", str(exc))
        else:
            flash(request, "success", "Регистрация закрыта")
    finally:
        conn.close()
    return RedirectResponse("/admin/registration", status_code=303)


@router.get("/registration/status")
def registration_status(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        state = registration.status(conn)
    finally:
        conn.close()
    return JSONResponse(state, headers={"Cache-Control": "no-store"})


@router.post("/registration/delete")
def registration_delete(
    request: Request,
    student_id: int = Form(0),
    confirmed: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        student = conn.execute(
            "SELECT s.*, g.enroll_until, g.enroll_window_started FROM students s "
            "JOIN groups g ON g.id = s.group_id WHERE s.id = ?",
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
        eligible = (
            registration.group_is_open(conn, student["group_id"])
            and student["enroll_window_started"]
            and student["created_at"] >= student["enroll_window_started"]
            and not works
            and not messages
        )
        if not eligible or confirmed != "1":
            return RedirectResponse(
                f"/admin/students/{student_id}/delete", status_code=303
            )
        group_id = student["group_id"]
        nick = student["nick"]
    finally:
        conn.close()
    nick = student_service.delete_student(request.app.state.settings, student_id)
    conn = request.app.state.db_connect()
    try:
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, details_json) "
            "VALUES (?, 'admin', 'student_deleted_quick', ?)",
            (utc_now_iso(), f'{{"group_id": {group_id}}}'),
        )
    finally:
        conn.close()
    flash(request, "success", f"Ник {nick} удалён")
    return RedirectResponse("/admin/registration", status_code=303)
