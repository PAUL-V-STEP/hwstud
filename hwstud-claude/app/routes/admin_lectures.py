from __future__ import annotations

import asyncio
import json
import shutil
import uuid
import zipfile
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from starlette.responses import FileResponse, RedirectResponse

from ..auth import check_csrf, flash
from ..db import transaction, utc_now_iso
from ..services import glossary, literature
from ..services.lecture_import import (
    ACTIVE_IMPORTS,
    expire_ready_imports,
    load_report,
    normalize_title,
)
from ..services.lecture_store import cancel_import, save_import
from ..web import render
from ..worker import enqueue_job
from .admin import require_admin

router = APIRouter(prefix="/admin/lectures")
MAX_LECTURE_FILE = 40 * 1024 * 1024
MAX_IMPORT_BYTES = 1024 * 1024 * 1024


async def _save_upload(upload: UploadFile, target: Path) -> int:
    size = 0
    with target.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_LECTURE_FILE:
                raise ValueError(f"Файл {upload.filename} больше 40 МБ")
            output.write(chunk)
    if size == 0:
        raise ValueError(f"Файл {upload.filename} пуст")
    return size


def _validate_initial_files(pptx: Path, pdf: Path) -> None:
    if not zipfile.is_zipfile(pptx):
        raise ValueError("PPTX не является корректным ZIP-пакетом")
    with pdf.open("rb") as source:
        if source.read(5) != b"%PDF-":
            raise ValueError("PDF имеет неверную сигнатуру")


def _import_disk_usage(settings) -> int:
    root = settings.data_dir / "import"
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) if root.exists() else 0


@router.get("")
def lectures_page(request: Request, actor=Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        lectures = conn.execute(
            "SELECT * FROM lectures ORDER BY num, id"
        ).fetchall()
        groups = conn.execute("SELECT * FROM groups ORDER BY id").fetchall()
        selected = {
            row["lecture_id"]: row["group_ids"].split(",") if row["group_ids"] else []
            for row in conn.execute(
                "SELECT lecture_id, GROUP_CONCAT(group_id) AS group_ids "
                "FROM lecture_groups GROUP BY lecture_id"
            )
        }
        imports = conn.execute(
            "SELECT * FROM imports WHERE status NOT IN ('saved', 'cancelled') "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
    finally:
        conn.close()
    return render(
        request,
        "admin_lectures.html",
        {
            "title": "Лекции",
            "lectures": lectures,
            "groups": groups,
            "selected": selected,
            "imports": imports,
        },
        actor=actor,
    )


@router.post("/import")
async def upload_lecture(
    request: Request,
    pptx: UploadFile = File(...),
    pdf: UploadFile = File(...),
    csrf: str = Form(""),
    actor=Depends(require_admin),
):
    check_csrf(request, csrf)
    settings = request.app.state.settings
    temporary = settings.data_dir / "tmp" / f"lecture-{uuid.uuid4().hex}"
    temporary.mkdir(parents=True)
    try:
        pptx_size, pdf_size = await asyncio.gather(
            _save_upload(pptx, temporary / "source.pptx"),
            _save_upload(pdf, temporary / "source.pdf"),
        )
        _validate_initial_files(temporary / "source.pptx", temporary / "source.pdf")
        conn = request.app.state.db_connect()
        try:
            with transaction(conn, immediate=True):
                placeholders = ",".join("?" for _ in ACTIVE_IMPORTS)
                active = conn.execute(
                    f"SELECT COUNT(*) FROM imports WHERE status IN ({placeholders})",
                    ACTIVE_IMPORTS,
                ).fetchone()[0]
                if active >= 5:
                    raise ValueError("Уже есть 5 активных импортов. Сохраните или отмените прежние")
                if _import_disk_usage(settings) + pptx_size + pdf_size > MAX_IMPORT_BYTES:
                    raise ValueError("Активные импорты превысят 1 ГБ. Сохраните или отмените прежние")
                cursor = conn.execute(
                    "INSERT INTO imports(created_at, status, report_json) "
                    "VALUES (?, 'processing', ?)",
                    (
                        utc_now_iso(),
                        json.dumps(
                            {"pptx_name": pptx.filename, "pdf_name": pdf.filename},
                            ensure_ascii=False,
                        ),
                    ),
                )
                import_id = int(cursor.lastrowid)
        finally:
            conn.close()
        target = settings.data_dir / "import" / str(import_id)
        target.mkdir(parents=True)
        shutil.move(str(temporary / "source.pptx"), target / "source.pptx")
        shutil.move(str(temporary / "source.pdf"), target / "source.pdf")
        enqueue_job(
            settings,
            "lecture_import",
            payload={"import_id": import_id},
        )
        flash(request, "success", f"Лекция {import_id} поставлена в очередь разбора")
        return RedirectResponse(f"/admin/lectures/import/{import_id}", status_code=303)
    except Exception as exc:
        flash(request, "error", str(exc))
        return RedirectResponse("/admin/lectures", status_code=303)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
        await pptx.close()
        await pdf.close()


@router.get("/import/{import_id}")
def import_preview(request: Request, import_id: int, actor=Depends(require_admin)):
    expire_ready_imports(request.app.state.settings)
    conn = request.app.state.db_connect()
    try:
        row = conn.execute(
            "SELECT * FROM imports WHERE id = ?", (import_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404)
        report = load_report(row)
        term_review = glossary.collect_term_proposals(request.app.state.settings, import_id)
        source_review = literature.build_source_review(request.app.state.settings, import_id)
        target = None
        if report.get("title_norm"):
            target = conn.execute(
                "SELECT id, title FROM lectures WHERE title_norm = ?",
                (report["title_norm"],),
            ).fetchone()
    finally:
        conn.close()
    return render(
        request,
        "admin_import.html",
        {
            "title": f"Импорт {import_id}",
            "import": row,
            "report": report,
            "target": target,
            "term_review": term_review,
            "source_review": source_review,
            "import_id": import_id,
        },
        actor=actor,
    )


@router.post("/import/{import_id}/confirm")
async def confirm_review(
    request: Request,
    import_id: int,
    actor=Depends(require_admin),
):
    settings = request.app.state.settings
    form = await request.form()
    check_csrf(request, str(form.get("csrf", "")))
    terms = glossary.collect_term_proposals(settings, import_id)
    sources = literature.build_source_review(settings, import_id)
    approved_terms = []
    approved_sources = []
    for item in terms:
        if not item["requires_review"]:
            variant = item["variants"][0]
            approved_terms.append(
                {
                    "term": item["term"],
                    "expansion": variant["expansion"],
                    "english": variant["english"],
                    "slide_numbers": item["slide_numbers"],
                }
            )
    try:
        for index_text in form.getlist("terms"):
            index = int(index_text)
            item = terms[index]
            if not item["requires_review"]:
                continue
            variant_index = int(form.get(f"variant_{index}", 0))
            variant = item["variants"][variant_index]
            entered_expansion = str(
                form.get(f"expansion_{index}", item["variants"][0]["expansion"])
            ).strip()
            entered_english = str(
                form.get(f"english_{index}", item["variants"][0]["english"])
            ).strip()
            original = item["variants"][0]
            expansion = (
                variant["expansion"].strip()
                if entered_expansion == original["expansion"].strip() and variant_index != 0
                else entered_expansion
            )
            english = (
                variant["english"].strip()
                if entered_english == original["english"].strip() and variant_index != 0
                else entered_english
            )
            if not expansion:
                raise ValueError(f"Укажите расшифровку термина {item['term']} или снимите его выбор")
            approved_terms.append(
                {
                    "term": item["term"],
                    "expansion": expansion,
                    "english": english,
                    "slide_numbers": item["slide_numbers"],
                }
            )
        for index_text in form.getlist("sources"):
            index = int(index_text)
            item = dict(sources[index])
            item["title"] = str(form.get(f"source_title_{index}", item["title"])).strip()
            item["author"] = str(form.get(f"source_author_{index}", item["author"])).strip()
            if not item["title"]:
                raise ValueError("У источника должно быть название")
            approved_sources.append(item)
    except (ValueError, IndexError) as exc:
        flash(request, "error", f"Проверьте выбранные записи: {exc}")
        return RedirectResponse(f"/admin/lectures/import/{import_id}", status_code=303)

    conn = request.app.state.db_connect()
    try:
        row = conn.execute(
            "SELECT * FROM imports WHERE id = ?", (import_id,)
        ).fetchone()
        if not row or row["status"] != "ready":
            flash(request, "error", "Подтверждать можно только готовый предпросмотр")
            return RedirectResponse(f"/admin/lectures/import/{import_id}", status_code=303)
        report = load_report(row)
        report["approved_terms"] = approved_terms
        report["approved_sources"] = approved_sources
        report["review_confirmed"] = True
        with transaction(conn):
            conn.execute(
                "UPDATE imports SET report_json = ? WHERE id = ?",
                (json.dumps(report, ensure_ascii=False), import_id),
            )
    finally:
        conn.close()
    flash(request, "success", "Состав глоссария и литературы подтверждён")
    return RedirectResponse(f"/admin/lectures/import/{import_id}", status_code=303)


@router.get("/import/{import_id}/image/{number}")
def import_image(
    request: Request, import_id: int, number: int, actor=Depends(require_admin)
):
    settings = request.app.state.settings
    path = (
        settings.data_dir / "import" / str(import_id) / "slides" / f"{number}.webp"
    ).resolve()
    root = (settings.data_dir / "import").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.post("/import/{import_id}/save")
async def save(
    request: Request,
    import_id: int,
    title: str = Form(""),
    csrf: str = Form(""),
    actor=Depends(require_admin),
):
    check_csrf(request, csrf)
    try:
        _id, slug, updated = await asyncio.to_thread(
            save_import, request.app.state.settings, import_id, title
        )
        flash(
            request,
            "success",
            "Лекция обновлена и остаётся в прежнем статусе" if updated else "Лекция сохранена скрытой",
        )
        return RedirectResponse(f"/lectures/{slug}", status_code=303)
    except Exception as exc:
        flash(request, "error", str(exc))
        return RedirectResponse(f"/admin/lectures/import/{import_id}", status_code=303)


@router.post("/import/{import_id}/cancel")
async def cancel(
    request: Request,
    import_id: int,
    csrf: str = Form(""),
    actor=Depends(require_admin),
):
    check_csrf(request, csrf)
    try:
        await asyncio.to_thread(cancel_import, request.app.state.settings, import_id)
        flash(request, "success", "Предпросмотр и исходные файлы удалены")
    except Exception as exc:
        flash(request, "error", str(exc))
    return RedirectResponse("/admin/lectures", status_code=303)


@router.post("/{lecture_id}/access")
def access(
    request: Request,
    lecture_id: int,
    title: str = Form(""),
    status: str = Form("hidden"),
    planned_date: str = Form(""),
    announce: str = Form(""),
    selected_groups: list[int] = Form([]),
    csrf: str = Form(""),
    actor=Depends(require_admin),
):
    check_csrf(request, csrf)
    title = title.strip()
    title_norm = normalize_title(title)
    if status not in {"hidden", "open"}:
        status = "hidden"
    planned_date = planned_date.strip() or None
    announce_enabled = announce == "1"
    try:
        if not title:
            raise ValueError("Введите название лекции")
        if planned_date:
            date.fromisoformat(planned_date)
        if announce_enabled and not planned_date:
            raise ValueError("Для анонса укажите плановую дату")
        conn = request.app.state.db_connect()
        try:
            with transaction(conn, immediate=True):
                lecture = conn.execute(
                    "SELECT * FROM lectures WHERE id = ?", (lecture_id,)
                ).fetchone()
                if not lecture:
                    raise ValueError("Лекция не найдена")
                duplicate = conn.execute(
                    "SELECT id FROM lectures WHERE title_norm = ? AND id != ?",
                    (title_norm, lecture_id),
                ).fetchone()
                if duplicate:
                    raise ValueError("Лекция с таким названием уже существует")
                valid_groups = {
                    row["id"] for row in conn.execute("SELECT id FROM groups")
                }
                if any(group not in valid_groups for group in selected_groups):
                    raise ValueError("Неизвестная группа")
                conn.execute(
                    "UPDATE lectures SET title = ?, title_norm = ?, status = ?, "
                    "planned_date = ?, announce = ?, updated_at = ? WHERE id = ?",
                    (
                        title,
                        title_norm,
                        status,
                        planned_date,
                        int(announce_enabled),
                        utc_now_iso(),
                        lecture_id,
                    ),
                )
                conn.execute(
                    "DELETE FROM lecture_groups WHERE lecture_id = ?", (lecture_id,)
                )
                conn.executemany(
                    "INSERT INTO lecture_groups(lecture_id, group_id) VALUES (?, ?)",
                    [(lecture_id, group) for group in selected_groups],
                )
                conn.execute(
                    "INSERT INTO audit_log(at, actor, action, object, details_json) "
                    "VALUES (?, 'admin', 'lecture_access', ?, ?)",
                    (
                        utc_now_iso(),
                        lecture["slug"],
                        json.dumps(
                            {
                                "title": title,
                                "status": status,
                                "groups": selected_groups,
                                "planned_date": planned_date,
                                "announce": announce_enabled,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
        finally:
            conn.close()
        flash(request, "success", "Настройки лекции обновлены")
    except Exception as exc:
        flash(request, "error", str(exc))
    return RedirectResponse("/admin/lectures", status_code=303)


@router.post("/{lecture_id}/student")
def student_mode(
    request: Request,
    lecture_id: int,
    csrf: str = Form(""),
    actor=Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        lecture = conn.execute(
            "SELECT slug FROM lectures WHERE id = ?", (lecture_id,)
        ).fetchone()
    finally:
        conn.close()
    if not lecture:
        flash(request, "error", "Лекция не найдена")
        return RedirectResponse("/admin/lectures", status_code=303)
    request.session["student_preview_lecture"] = lecture_id
    return RedirectResponse(f"/lectures/{lecture['slug']}", status_code=303)
