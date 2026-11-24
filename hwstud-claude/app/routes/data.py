from __future__ import annotations

import asyncio
import json
import secrets
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import FileResponse, RedirectResponse

from ..auth import Actor, check_csrf, flash
from ..db import transaction, utc_now_iso
from ..services import backup
from ..web import render
from ..worker import enqueue_job
from .admin import require_admin

router = APIRouter(prefix="/admin/data")


def _safe_data_file(settings, relative: str, folder: str) -> Path:
    root = (settings.data_dir / folder).resolve()
    path = (settings.data_dir / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404)
    return path


@router.get("")
def data_page(request: Request, actor: Actor = Depends(require_admin)):
    settings = request.app.state.settings
    conn = request.app.state.db_connect()
    try:
        latest = conn.execute(
            "SELECT value FROM settings WHERE key = 'latest_works_archive'"
        ).fetchone()
        check = conn.execute(
            "SELECT value FROM settings WHERE key = 'last_archive_check'"
        ).fetchone()
        maintenance = conn.execute("SELECT * FROM maintenance WHERE id = 1").fetchone()
        active_export = conn.execute(
            "SELECT id FROM jobs WHERE kind = 'export' AND status IN ('queued', 'running')"
        ).fetchone()
    finally:
        conn.close()
    archives = sorted(
        (settings.data_dir / "archives").glob("site-*.tar.gz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if (settings.data_dir / "archives").exists() else []
    backups = list((settings.data_dir / "backups").glob("*.db")) if (settings.data_dir / "backups").exists() else []
    disk = shutil.disk_usage(settings.data_dir)
    return render(
        request,
        "admin_data.html",
        {
            "title": "Данные курса",
            "archives": archives,
            "backups_count": len(backups),
            "works_archive": latest["value"] if latest else "",
            "archive_check": json.loads(check["value"]) if check else None,
            "maintenance": maintenance,
            "active_export": active_export,
            "disk_free": disk.free,
            "disk_total": disk.total,
            "disk_used_percent": round(disk.used / disk.total * 100, 1),
        },
        actor=actor,
    )


@router.post("/backup")
async def create_backup(request: Request, csrf: str = Form(""), actor: Actor = Depends(require_admin)):
    check_csrf(request, csrf)
    await asyncio.to_thread(backup.create_site_archive, request.app.state.settings)
    flash(request, "success", "Новая копия базы и лекций создана")
    return RedirectResponse("/admin/data", status_code=303)


@router.get("/download")
def download_backup(request: Request, actor: Actor = Depends(require_admin)):
    settings = request.app.state.settings
    archives = sorted(
        (settings.data_dir / "archives").glob("site-*.tar.gz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not archives:
        raise HTTPException(status_code=404)
    return FileResponse(archives[0], filename=archives[0].name)


@router.post("/works")
def create_works(request: Request, csrf: str = Form(""), actor: Actor = Depends(require_admin)):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        active = conn.execute(
            "SELECT 1 FROM jobs WHERE kind = 'export' AND status IN ('queued', 'running')"
        ).fetchone()
    finally:
        conn.close()
    if active:
        flash(request, "error", "Архив работ уже собирается")
    else:
        enqueue_job(request.app.state.settings, "export", payload={"kind": "works"})
        flash(request, "success", "Архив работ поставлен в очередь. Временный файл удалится через час после создания")
    return RedirectResponse("/admin/data", status_code=303)


@router.get("/works/download")
def download_works(request: Request, actor: Actor = Depends(require_admin)):
    settings = request.app.state.settings
    conn = request.app.state.db_connect()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'latest_works_archive'"
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["value"]:
        raise HTTPException(status_code=404)
    path = _safe_data_file(settings, row["value"], "tmp")
    return FileResponse(path, filename=path.name)


@router.post("/check")
async def check_archive(request: Request, csrf: str = Form(""), actor: Actor = Depends(require_admin)):
    check_csrf(request, csrf)
    result = await asyncio.to_thread(backup.check_latest_archive, request.app.state.settings)
    flash(
        request,
        "success" if result["status"] == "ok" else "error",
        "Архив распакован, база цела" if result["status"] == "ok" else result.get("error", "Ошибка архива"),
    )
    return RedirectResponse("/admin/data", status_code=303)


@router.get("/delete")
def delete_first(request: Request, actor: Actor = Depends(require_admin)):
    return render(request, "admin_delete.html", {"title": "Удаление данных студентов", "second": False}, actor=actor)


@router.post("/delete")
def delete_first_confirm(
    request: Request,
    confirm_first: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    if confirm_first != "1":
        flash(request, "error", "Подтвердите первый этап")
        return RedirectResponse("/admin/data/delete", status_code=303)
    request.session["delete_confirmation_token"] = secrets.token_urlsafe(32)
    return render(
        request,
        "admin_delete.html",
        {"title": "Окончательное подтверждение", "second": True},
        actor=actor,
    )


@router.post("/delete/confirm")
def delete_final(
    request: Request,
    confirmation: str = Form(""),
    token: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    expected = request.session.pop("delete_confirmation_token", None)
    if not expected or token != expected or confirmation != "УДАЛИТЬ":
        flash(request, "error", "Операция отменена: второе подтверждение не совпало")
        return RedirectResponse("/admin/data", status_code=303)
    try:
        backup.start_student_deletion(request.app.state.settings)
    except ValueError as exc:
        flash(request, "error", str(exc))
        return RedirectResponse("/admin/data", status_code=303)
    flash(request, "success", "Удаление началось. До завершения студенческие маршруты закрыты")
    return RedirectResponse("/admin/data/deletion", status_code=303)


@router.get("/deletion")
def deletion_status(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        state = conn.execute("SELECT * FROM maintenance WHERE id = 1").fetchone()
        job = conn.execute(
            "SELECT * FROM jobs WHERE kind = 'delete_students' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return render(
        request,
        "admin_deletion.html",
        {"title": "Удаление данных студентов", "state": state, "job": job},
        actor=actor,
    )
