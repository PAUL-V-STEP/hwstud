from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse, Response

from ..access import content_visible, exam_active, lecture_visible
from ..auth import Actor, check_csrf, flash
from ..db import utc_now
from ..services import exports, glossary, literature
from ..web import render
from .admin import require_admin
from .student import require_user

router = APIRouter()


def _closed(request: Request, actor: Actor) -> bool:
    if actor.is_admin:
        return False
    conn = request.app.state.db_connect()
    try:
        return not content_visible(conn, actor.group_id, utc_now())
    finally:
        conn.close()


def _closed_response(request: Request, actor: Actor):
    return render(request, "content_closed.html", {"title": "Материалы закрыты"}, actor=actor)


@router.get("/glossary")
def glossary_page(
    request: Request,
    q: str = "",
    lecture: str = "",
    actor: Actor = Depends(require_user),
):
    if _closed(request, actor):
        return _closed_response(request, actor)
    conn = request.app.state.db_connect()
    try:
        terms = glossary.visible_terms(conn, actor, lecture_slug=lecture or None, query=q.strip())
    finally:
        conn.close()
    return render(
        request,
        "glossary.html",
        {
            "title": "Глоссарий",
            "terms": terms,
            "q": q,
            "lecture": lecture,
            "powerpoint": glossary.powerpoint_block(terms),
        },
        actor=actor,
    )


@router.get("/glossary/powerpoint.txt")
def glossary_txt(request: Request, actor: Actor = Depends(require_user)):
    if _closed(request, actor):
        raise HTTPException(status_code=404)
    conn = request.app.state.db_connect()
    try:
        terms = glossary.visible_terms(conn, actor)
    finally:
        conn.close()
    return Response(
        glossary.powerpoint_block(terms),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="abbreviations.txt"'},
    )


@router.get("/glossary/export/{fmt}")
async def glossary_export(
    request: Request, fmt: str, actor: Actor = Depends(require_user)
):
    if _closed(request, actor):
        raise HTTPException(status_code=404)
    conn = request.app.state.db_connect()
    try:
        terms = glossary.visible_terms(conn, actor)
    finally:
        conn.close()
    try:
        content, media_type, filename = await asyncio.to_thread(
            exports.export_records, request.app.state.settings, "glossary", terms, fmt
        )
    except ValueError as exc:
        raise HTTPException(status_code=404) from exc
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/literature")
def literature_page(
    request: Request,
    q: str = "",
    lecture: str = "",
    priority: str = "",
    view: str = "list",
    actor: Actor = Depends(require_user),
):
    if _closed(request, actor):
        return _closed_response(request, actor)
    conn = request.app.state.db_connect()
    try:
        sources = literature.visible_sources(
            conn,
            actor,
            lecture_slug=lecture or None,
            query=q.strip(),
            priority=priority,
        )
    finally:
        conn.close()
    if view == "topics":
        sources = sorted(sources, key=lambda row: row["type"] or "")
    return render(
        request,
        "literature.html",
        {
            "sources": sources,
            "q": q,
            "lecture": lecture,
            "priority": priority,
            "view": view if view in {"list", "lectures", "topics"} else "list",
        },
        actor=actor,
    )


@router.get("/literature/{slug}/source/{number}")
def literature_citation(
    request: Request,
    slug: str,
    number: int,
    actor: Actor = Depends(require_user),
):
    conn = request.app.state.db_connect()
    try:
        row = conn.execute(
            "SELECT s.id, l.* FROM sources s JOIN source_refs sr ON sr.source_id = s.id "
            "JOIN lectures l ON l.id = sr.lecture_id WHERE l.slug = ? AND sr.local_number = ?",
            (slug, number),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404)
        if not actor.is_admin:
            if exam_active(conn, actor.group_id, utc_now()) or not lecture_visible(
                conn, actor, row, utc_now()
            ):
                raise HTTPException(status_code=404)
    finally:
        conn.close()
    return RedirectResponse(
        f"/literature?lecture={slug}#source-{row['id']}", status_code=303
    )


@router.get("/literature/export/{fmt}")
async def literature_export(
    request: Request, fmt: str, actor: Actor = Depends(require_user)
):
    if _closed(request, actor):
        raise HTTPException(status_code=404)
    conn = request.app.state.db_connect()
    try:
        sources = literature.visible_sources(conn, actor)
    finally:
        conn.close()
    try:
        content, media_type, filename = await asyncio.to_thread(
            exports.export_records, request.app.state.settings, "literature", sources, fmt
        )
    except ValueError as exc:
        raise HTTPException(status_code=404) from exc
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/admin/glossary")
def admin_glossary(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        terms = conn.execute(
            "SELECT t.*, COUNT(tr.lecture_id) AS refs FROM terms t "
            "LEFT JOIN term_refs tr ON tr.term_id = t.id GROUP BY t.id ORDER BY lower(t.term)"
        ).fetchall()
    finally:
        conn.close()
    return render(
        request,
        "admin_glossary.html",
        {"title": "Глоссарий", "terms": terms},
        actor=actor,
    )


@router.post("/admin/glossary/{term_id}")
def edit_term(
    request: Request,
    term_id: int,
    term: str = Form(""),
    expansion: str = Form(""),
    english: str = Form(""),
    status: str = Form("draft"),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    if status not in {"draft", "verified", "hidden"}:
        status = "draft"
    conn = request.app.state.db_connect()
    try:
        conn.execute(
            "UPDATE terms SET term = ?, expansion = ?, english = ?, status = ?, "
            "unused = NOT EXISTS(SELECT 1 FROM term_refs WHERE term_id = ?) WHERE id = ?",
            (
                term.strip(), expansion.strip(), english.strip(), status,
                term_id, term_id,
            ),
        )
    finally:
        conn.close()
    flash(request, "success", "Термин обновлён")
    return RedirectResponse("/admin/glossary", status_code=303)


@router.post("/admin/glossary/{term_id}/delete")
def delete_term(
    request: Request,
    term_id: int,
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        cursor = conn.execute("DELETE FROM terms WHERE id = ?", (term_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404)
    finally:
        conn.close()
    flash(request, "success", "Термин удалён")
    return RedirectResponse("/admin/glossary", status_code=303)


@router.get("/admin/literature")
def admin_literature(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        sources = conn.execute(
            "SELECT * FROM sources ORDER BY lower(title)"
        ).fetchall()
    finally:
        conn.close()
    return render(
        request,
        "admin_literature.html",
        {"title": "Литература", "sources": sources},
        actor=actor,
    )


@router.post("/admin/literature/{source_id}")
def edit_source(
    request: Request,
    source_id: int,
    type: str = Form(""),
    annotation: str = Form(""),
    priority: str = Form("extra"),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    if priority not in {"main", "extra"}:
        priority = "extra"
    conn = request.app.state.db_connect()
    try:
        conn.execute(
            "UPDATE sources SET type = ?, annotation = ?, priority = ? WHERE id = ?",
            (type.strip(), annotation.strip(), priority, source_id),
        )
    finally:
        conn.close()
    flash(request, "success", "Источник обновлён")
    return RedirectResponse("/admin/literature", status_code=303)


@router.post("/admin/literature/{source_id}/delete")
def delete_source(
    request: Request,
    source_id: int,
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        cursor = conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404)
    finally:
        conn.close()
    flash(request, "success", "Источник удалён")
    return RedirectResponse("/admin/literature", status_code=303)
