from __future__ import annotations

import json

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
    normalize_nick,
    validate_nick,
    verify_admin_password,
)
from ..db import utc_now_iso
from ..web import render
from ..services import backup, personal_keys, registration
from ..services.access_cards import access_card_text
from ..services.rate_limits import (
    check_login_attempt,
    check_registration,
    record_registration,
    reset_login_nick,
)

router = APIRouter()


def _login_response(
    request: Request,
    *,
    nick: str = "",
    show_groups: bool = False,
    show_key: bool = False,
    error: str = "",
    status_code: int = 200,
):
    conn = request.app.state.db_connect()
    try:
        groups = registration.open_groups(conn) if show_groups else []
        if show_groups and not groups and not error:
            error = (
                "Регистрация сейчас закрыта. Её открывает преподаватель на занятии. "
                "Если вы пропустили занятие, напишите преподавателю — он создаст ник и пришлёт ключ"
            )
    finally:
        conn.close()
    return render(
        request,
        "login.html",
        {
            "title": "Вход студента",
            "nick": nick,
            "show_groups": show_groups,
            "show_key": show_key,
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
    personal_key: str = Form(""),
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
    nick = nick.strip()
    validation_error = validate_nick(nick)
    nick_norm = normalize_nick(nick) if not validation_error else None
    limit_error, pair_key = check_login_attempt(limiter, client_ip(request), nick_norm)
    if limit_error:
        return _login_response(request, nick=nick, error=limit_error, status_code=429)
    if validation_error:
        return _login_response(request, error=validation_error, status_code=400)
    conn = request.app.state.db_connect()
    try:
        student = find_student(conn, nick)
        if student and student["blocked"]:
            return _login_response(
                request,
                nick=nick,
                error="Доступ для этого ника заблокирован преподавателем.",
                status_code=403,
            )
        if student:
            if not student["key_hash"]:
                return _login_response(
                    request, nick=nick, show_key=True,
                    error="Для этого ника ещё не выдан ключ. Обратитесь к преподавателю",
                    status_code=403,
                )
            if not personal_key.strip():
                return _login_response(request, nick=nick, show_key=True)
            key_state = personal_keys.check_student_key(conn, student, personal_key)
            if key_state == "locked":
                return _login_response(
                    request, nick=nick, show_key=True,
                    error="Слишком много неверных попыток для этого ника. "
                    "Повторите через 15 минут",
                    status_code=423,
                )
            if key_state != "ok":
                return _login_response(
                    request, nick=nick, show_key=True,
                    error="Ник или ключ не подходят", status_code=403,
                )
            reset_login_nick(limiter, pair_key)
            login_student(request, student)
            return RedirectResponse("/", status_code=303)
        if group_id is None:
            return _login_response(
                request,
                nick=nick,
                show_groups=True,
            )
        registration_error = check_registration(limiter, client_ip(request))
        if registration_error:
            return _login_response(
                request, nick=nick, show_groups=True,
                error=registration_error, status_code=429,
            )
        student, error = create_student(conn, nick, group_id, require_window=True)
        if error:
            return _login_response(
                request,
                nick=nick,
                show_groups=True,
                error=error,
                status_code=400,
            )
        record_registration(limiter, client_ip(request))
        reset_login_nick(limiter, pair_key)
        key = personal_keys.issue_key(conn, student["id"], "student")
        student = find_student(conn, nick)
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, student_id, details_json) "
            "VALUES (?, 'student', 'student_registered', ?, ?)",
            (
                utc_now_iso(), student["id"],
                json.dumps({"group_id": group_id, "via": "window"}, ensure_ascii=False),
            ),
        )
        login_student(request, student)
        card_id = request.app.state.access_cards.create(
            nick=student["nick"], key=key, audience="student",
            student_id=student["id"], next_path="/",
        )
        return RedirectResponse(f"/card/{card_id}", status_code=303)
    finally:
        conn.close()


def _card_text(request: Request, card: dict) -> str:
    return access_card_text(
        request.app.state.settings.app_base_url, card["nick"], card["key"]
    )


@router.get("/card/{card_id}")
def access_card(request: Request, card_id: str):
    actor = current_actor(request)
    card = request.app.state.access_cards.get(card_id, actor)
    if not card:
        raise HTTPException(status_code=404)
    response = render(
        request,
        "access_card.html",
        {
            "title": "Карточка доступа", "card": card,
            "card_text": _card_text(request, card), "back_href": "",
        },
        actor=actor,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/card/{card_id}/saved")
def save_access_card(request: Request, card_id: str, csrf: str = Form("")):
    check_csrf(request, csrf)
    actor = current_actor(request)
    card = request.app.state.access_cards.get(card_id, actor)
    if not card or not request.app.state.access_cards.delete(card_id, actor):
        raise HTTPException(status_code=404)
    flash(request, "success", "Карточка доступа закрыта")
    return RedirectResponse(card["next_path"], status_code=303)


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
