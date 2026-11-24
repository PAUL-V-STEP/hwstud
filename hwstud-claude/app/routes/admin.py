from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from ..auth import Actor, change_admin_password, check_csrf, client_ip, current_actor, flash
from ..services.mailer import Mailer
from ..services import backup
from ..services import assignments as assignment_service
from ..services.proxyapi import Outcome, ProxyAPIClient
from ..db import transaction, utc_now_iso
from ..web import render
from ..worker import enqueue_job

router = APIRouter(prefix="/admin")


def require_admin(request: Request) -> Actor:
    actor = current_actor(request)
    if not actor:
        raise HTTPException(status_code=401)
    if not actor.is_admin:
        raise HTTPException(status_code=404)
    if (
        request.session.get("must_change_password")
        and request.url.path != "/admin/settings/password"
    ):
        raise HTTPException(
            status_code=303,
            headers={"Location": "/admin/settings/password"},
        )
    if (
        backup.student_deletion_active(request.app.state.settings)
        and request.url.path != "/admin/data/deletion"
    ):
        raise HTTPException(status_code=503)
    return actor


@router.get("")
def dashboard(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        students = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        groups = conn.execute(
            "SELECT g.name, COUNT(s.id) AS members FROM groups g "
            "LEFT JOIN students s ON s.group_id = g.id GROUP BY g.id ORDER BY g.id"
        ).fetchall()
        jobs = conn.execute(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
        ).fetchall()
    finally:
        conn.close()
    snapshot = request.app.state.balance.snapshot()
    return render(
        request,
        "admin_dashboard.html",
        {
            "title": "Панель преподавателя",
            "students": students,
            "groups": groups,
            "jobs": jobs,
            "balance": snapshot,
        },
        actor=actor,
    )


@router.get("/settings")
def settings_page(request: Request, actor: Actor = Depends(require_admin)):
    settings = request.app.state.settings
    conn = request.app.state.db_connect()
    try:
        mail_mode = conn.execute(
            "SELECT value FROM settings WHERE key = 'mail_mode'"
        ).fetchone()["value"]
        distribution_enabled = conn.execute(
            "SELECT value FROM settings WHERE key = 'distribution_enabled'"
        ).fetchone()["value"] == "1"
        grading_instructions = conn.execute(
            "SELECT value FROM settings WHERE key = 'grading_instructions'"
        ).fetchone()
        stored_password = conn.execute(
            "SELECT value FROM settings WHERE key = 'admin_password_hash'"
        ).fetchone()
        password_changed = conn.execute(
            "SELECT value FROM settings WHERE key = 'admin_password_changed'"
        ).fetchone()
    finally:
        conn.close()
    return render(
        request,
        "admin_settings.html",
        {
            "title": "Настройки",
            "smtp_configured": settings.smtp_configured,
            "smtp_host": settings.smtp_host,
            "mail_to": settings.mail_to,
            "mail_mode": mail_mode,
            "distribution_enabled": distribution_enabled,
            "grading_instructions": grading_instructions["value"] if grading_instructions else "",
            "password_is_default": (
                not stored_password
                and not settings.admin_password_hash
                and (not password_changed or password_changed["value"] != "1")
            ),
        },
        actor=actor,
    )


@router.post("/settings/test-email")
def test_email(
    request: Request,
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    settings = request.app.state.settings
    if not settings.smtp_configured:
        flash(
            request,
            "error",
            "SMTP не настроен. Заполните параметры в .env и перезапустите приложение.",
        )
    else:
        job_id = enqueue_job(
            settings,
            "mail",
            payload={
                "subject": "hwstud: пробное письмо",
                "body": "Локальная проверка SMTP сайта hwstud.ru прошла через очередь задач.",
            },
        )
        flash(request, "success", f"Пробное письмо поставлено в очередь (задача {job_id}).")
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/settings/mail-mode")
def mail_mode(
    request: Request,
    mode: str = Form("immediate"),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    if mode not in {"immediate", "hourly"}:
        raise HTTPException(status_code=400)
    conn = request.app.state.db_connect()
    try:
        conn.execute("UPDATE settings SET value = ? WHERE key = 'mail_mode'", (mode,))
    finally:
        conn.close()
    flash(request, "success", "Режим писем о сообщениях сохранён")
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/settings/grading-instructions")
def grading_instructions(
    request: Request,
    instructions: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    instructions = instructions.strip()
    conn = request.app.state.db_connect()
    try:
        try:
            assignments = assignment_service.active_assignments(conn)
            largest_assignment = max(
                (
                    sum(len(assignment[field]) for field in (
                        "condition", "reference", "criteria", "typical_errors"
                    ))
                    for assignment in assignments
                ),
                default=0,
            )
            if len(instructions) + largest_assignment > 12_000:
                raise ValueError(
                    "Общие инструкции и тексты самого большого задания суммарно "
                    "превышают 12 000 знаков"
                )
            with transaction(conn, immediate=True):
                cursor = conn.execute(
                    "UPDATE settings SET value = ? WHERE key = 'grading_instructions'",
                    (instructions,),
                )
                if cursor.rowcount == 0:
                    conn.execute(
                        "INSERT INTO settings(key, value) VALUES ('grading_instructions', ?)",
                        (instructions,),
                    )
                conn.execute(
                    "INSERT INTO audit_log(at, actor, action, object) "
                    "VALUES (?, 'admin', 'grading_instructions_updated', 'settings')",
                    (utc_now_iso(),),
                )
        except ValueError as exc:
            flash(request, "error", str(exc))
        else:
            flash(request, "success", "Общие инструкции сохранены для следующих проверок")
    finally:
        conn.close()
    return RedirectResponse("/admin/settings#grading-instructions", status_code=303)


@router.get("/settings/password")
def password_page(request: Request, actor: Actor = Depends(require_admin)):
    return render(
        request,
        "admin_password.html",
        {
            "title": "Смена пароля",
            "must_change": bool(request.session.get("must_change_password")),
        },
        actor=actor,
    )


@router.post("/settings/password")
def change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirmation: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    limiter = request.app.state.rate_limiter
    key = f"admin-password-change:{client_ip(request)}"
    if not limiter.allowed(key, 5, 900):
        flash(request, "error", "Слишком много ошибок. Повторите через 15 минут")
        return RedirectResponse("/admin/settings/password", status_code=303)
    conn = request.app.state.db_connect()
    try:
        change_admin_password(conn=conn, request=request, current_password=current_password, new_password=new_password, confirmation=confirmation)
    except ValueError as exc:
        if str(exc) == "Текущий пароль неверен":
            limiter.register(key, 900)
        flash(request, "error", str(exc))
        return RedirectResponse("/admin/settings/password", status_code=303)
    finally:
        conn.close()
    limiter.reset(key)
    flash(request, "success", "Пароль изменён. Для следующего входа используйте новый пароль")
    return RedirectResponse("/admin", status_code=303)


@router.get("/settings/proxyapi")
def proxyapi_page(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        config = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('balance_low', 'balance_critical', 'reserve_factor', 'image_tokens', "
                "'price_grading_in', 'price_grading_out', 'price_aux_in', 'price_aux_out', 'billing_url')"
            )
        }
    finally:
        conn.close()
    return render(
        request,
        "admin_proxyapi.html",
        {
            "title": "ProxyAPI",
            "balance": request.app.state.balance.snapshot(),
            "config": config,
            "key_configured": bool(request.app.state.settings.proxyapi_key),
            "model_grading": request.app.state.settings.model_grading,
            "model_aux": request.app.state.settings.model_aux,
        },
        actor=actor,
    )


@router.post("/settings/proxyapi/check")
async def proxyapi_check(
    request: Request,
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    report = await request.app.state.balance.check(source="manual")
    flash(
        request,
        "success" if report.ok else "error",
        (
            f"Баланс: {report.balance:.2f} ₽."
            if report.ok and report.balance is not None
            else report.error or "Не удалось проверить баланс."
        ),
    )
    return RedirectResponse("/admin/settings/proxyapi", status_code=303)


@router.post("/settings/proxyapi/resume")
async def proxyapi_resume(
    request: Request,
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    balance = request.app.state.balance
    if balance.snapshot()["state"]["balance_access"] == "allowed":
        report = await balance.check(source="manual")
        resumed = report.ok and not report.paused
    else:
        resumed = await request.app.state.worker.run_probe()
    flash(
        request,
        "success" if resumed else "error",
        (
            "Пауза снята, задачи возвращены в очередь."
            if resumed
            else "Снять паузу пока нельзя: проверьте баланс и доступ ключа."
        ),
    )
    return RedirectResponse("/admin/settings/proxyapi", status_code=303)


@router.post("/settings/proxyapi/config")
def proxyapi_config(
    request: Request,
    balance_low: str = Form("300"),
    balance_critical: str = Form("100"),
    reserve_factor: str = Form("1.5"),
    image_tokens: str = Form("1500"),
    price_grading_in: str = Form("260"),
    price_grading_out: str = Form("1300"),
    price_aux_in: str = Form("30"),
    price_aux_out: str = Form("150"),
    billing_url: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    try:
        values = {
            "balance_low": balance_low,
            "balance_critical": balance_critical,
            "reserve_factor": reserve_factor,
            "image_tokens": image_tokens,
            "price_grading_in": price_grading_in,
            "price_grading_out": price_grading_out,
            "price_aux_in": price_aux_in,
            "price_aux_out": price_aux_out,
        }
        numeric = {key: float(value) for key, value in values.items()}
        if numeric["balance_low"] <= numeric["balance_critical"] or numeric["balance_critical"] < 0:
            raise ValueError("Порог «мало» должен быть больше порога «критично»")
        if numeric["reserve_factor"] < 1 or numeric["image_tokens"] < 1:
            raise ValueError("Резерв не меньше 1, image_tokens больше нуля")
        if any(numeric[key] < 0 for key in numeric if key.startswith("price_")):
            raise ValueError("Цены не могут быть отрицательными")
        if billing_url and not billing_url.startswith("https://"):
            raise ValueError("Ссылка на пополнение должна начинаться с https://")
        conn = request.app.state.db_connect()
        try:
            for key, value in numeric.items():
                conn.execute(
                    "UPDATE settings SET value = ? WHERE key = ?",
                    (str(value), key),
                )
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'billing_url'",
                (billing_url.strip(),),
            )
        finally:
            conn.close()
        flash(request, "success", "Пороги, цены и ссылка сохранены")
    except ValueError as exc:
        flash(request, "error", str(exc))
    return RedirectResponse("/admin/settings/proxyapi", status_code=303)


@router.post("/settings/proxyapi/test-model")
async def proxyapi_test_model(
    request: Request,
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    settings = request.app.state.settings
    client = ProxyAPIClient(settings)
    result = await client.chat_completion(
        model=settings.model_grading,
        messages=[{"role": "user", "content": "Верни JSON-объект {\"answer\":\"ok\"}."}],
        max_completion_tokens=100,
        reasoning_effort="low",
        response_format={"type": "json_object"},
    )
    if result.outcome == Outcome.NO_FUNDS:
        request.app.state.balance.pause("ProxyAPI ответил 402 на пробный запрос")
    elif result.outcome == Outcome.KEY_REJECTED:
        request.app.state.balance.reject_key(result.error)
    flash(
        request,
        "success" if result.ok else "error",
        "Модель ответила корректным JSON" if result.ok else result.error,
    )
    return RedirectResponse("/admin/settings/proxyapi", status_code=303)
