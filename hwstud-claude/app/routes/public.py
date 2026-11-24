from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from starlette import status
from starlette.responses import RedirectResponse

from ..auth import (
    check_csrf,
    client_ip,
    create_student,
    current_actor,
    find_student,
    flash,
    hash_password,
    login_admin,
    login_student,
    validate_nick,
    verify_admin_password,
)
from ..web import render
from ..services import backup

router = APIRouter()


def _login_response(
    request: Request,
    *,
    nick: str = "",
    show_groups: bool = False,
    error: str = "",
    status_code: int = 200,
):
    conn = request.app.state.db_connect()
    try:
        groups = conn.execute(
            "SELECT * FROM groups ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return render(
        request,
        "login.html",
        {
            "title": "Вход студента",
            "nick": nick,
            "show_groups": show_groups,
            "groups": groups,
            "error": error,
        },
        status_code=status_code,
        actor=None,
    )


@router.get("/login")
def login_page(request: Request):
    actor = current_actor(request)
    if actor:
        return RedirectResponse("/admin" if actor.is_admin else "/", status_code=303)
    return _login_response(request)


@router.post("/login")
def login_submit(
    request: Request,
    nick: str = Form(""),
    group_id: int | None = Form(None),
    csrf: str = Form(""),
):
    check_csrf(request, csrf)
    if backup.student_deletion_active(request.app.state.settings):
        return _login_response(
            request,
            nick=nick,
            error="Идёт завершение курса и удаление данных. Вход временно закрыт.",
            status_code=503,
        )
    limiter = request.app.state.rate_limiter
    key = f"student:{client_ip(request)}"
    if not limiter.allowed(key, 20, 3600):
        return _login_response(
            request,
            nick=nick,
            error="Слишком много попыток входа. Повторите через час.",
            status_code=429,
        )
    limiter.register(key, 3600)
    nick = nick.strip()
    error = validate_nick(nick)
    if error:
        return _login_response(request, error=error, status_code=400)
    conn = request.app.state.db_connect()
    try:
        student = find_student(conn, nick)
        if student and not student["blocked"]:
            login_student(request, student)
            return RedirectResponse("/", status_code=303)
        if student and student["blocked"]:
            return _login_response(
                request,
                nick=nick,
                error="Доступ для этого ника заблокирован преподавателем.",
                status_code=403,
            )
        if group_id is None:
            return _login_response(
                request,
                nick=nick,
                show_groups=True,
            )
        student, error = create_student(conn, nick, group_id)
        if error:
            return _login_response(
                request,
                nick=nick,
                show_groups=True,
                error=error,
                status_code=400,
            )
        login_student(request, student)
        return RedirectResponse("/", status_code=303)
    finally:
        conn.close()


@router.get("/admin/login")
def admin_login_page(request: Request):
    actor = current_actor(request)
    if actor and actor.is_admin:
        return RedirectResponse("/admin", status_code=303)
    return render(
        request,
        "admin_login.html",
        {"title": "Вход преподавателя"},
        actor=None,
    )


@router.post("/admin/login")
def admin_login_submit(
    request: Request,
    password: str = Form(""),
    csrf: str = Form(""),
):
    check_csrf(request, csrf)
    limiter = request.app.state.rate_limiter
    key = f"admin:{client_ip(request)}"
    if not limiter.allowed(key, 5, 900):
        return render(
            request,
            "admin_login.html",
            {
                "title": "Вход преподавателя",
                "error": "Слишком много ошибок. Повторите через 15 минут.",
            },
            status_code=429,
            actor=None,
        )
    conn = request.app.state.db_connect()
    try:
        valid, used_default = verify_admin_password(
            request.app.state.settings, conn, password
        )
    finally:
        conn.close()
    if not valid:
        limiter.register(key, 900)
        return render(
            request,
            "admin_login.html",
            {"title": "Вход преподавателя", "error": "Неверный пароль."},
            status_code=403,
            actor=None,
        )
    limiter.reset(key)
    login_admin(request, must_change_password=used_default)
    if used_default:
        return RedirectResponse("/admin/settings/password", status_code=303)
    return RedirectResponse("/admin", status_code=303)


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/about")
def about(request: Request):
    conn = request.app.state.db_connect()
    try:
        lectures = conn.execute(
            "SELECT title FROM lectures WHERE status = 'open' ORDER BY num, id"
        ).fetchall()
        about_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'about_text'"
        ).fetchone()
    finally:
        conn.close()
    return render(
        request,
        "about.html",
        {
            "title": "О курсе и авторе",
            "lectures": lectures,
            "about_text": about_row["value"] if about_row else "",
        },
    )
