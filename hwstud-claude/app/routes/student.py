from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import FileResponse, RedirectResponse

from ..access import (
    can_view_help,
    content_visible,
    exam_active,
    help_view,
    lecture_visible,
)
from ..auth import Actor, current_actor
from ..db import utc_now
from ..services import glossary
from ..services import assignments as assignment_service
from ..services import communication as communication_service
from ..services import cabinet as cabinet_service
from ..services import backup
from ..services.submissions import STUDENT_STATUS
from ..web import render

router = APIRouter()


def require_user(request: Request) -> Actor:
    actor = current_actor(request)
    if not actor:
        raise HTTPException(status_code=401)
    if not actor.is_admin and backup.student_deletion_active(request.app.state.settings):
        raise HTTPException(status_code=503)
    return actor


@router.get("/")
def home(request: Request, actor: Actor = Depends(require_user)):
    if actor.is_admin:
        return RedirectResponse("/admin", status_code=303)
    conn = request.app.state.db_connect()
    try:
        assignments = assignment_service.active_assignments(conn)
        latest_submissions = {
            assignment["id"]: conn.execute(
                "SELECT * FROM submissions WHERE student_id = ? AND assignment_id = ? "
                "ORDER BY attempt DESC LIMIT 1",
                (actor.student_id, assignment["id"]),
            ).fetchone()
            for assignment in assignments
        }
        accepted_count = sum(
            1 for row in latest_submissions.values() if row and row["status"] == "accepted"
        )
        lectures = []
        if content_visible(conn, actor.group_id, utc_now()):
            lectures = conn.execute(
                "SELECT * FROM lectures WHERE status = 'open' AND ("
                "NOT EXISTS (SELECT 1 FROM lecture_groups lg WHERE lg.lecture_id = lectures.id) "
                "OR EXISTS (SELECT 1 FROM lecture_groups lg WHERE lg.lecture_id = lectures.id "
                "AND lg.group_id = ?)) ORDER BY num, id",
                (actor.group_id,),
            ).fetchall()
        announcement_items = [
            {
                "announcement": row,
                "link": communication_service.resolve_announcement_link(conn, actor, row["link"]),
            }
            for row in communication_service.visible_announcements(conn, actor, limit=3)
        ]
        last_answer = conn.execute(
            "SELECT * FROM messages WHERE student_id = ? AND author = 'teacher' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (actor.student_id,),
        ).fetchone()
    finally:
        conn.close()
    return render(
        request,
        "dashboard.html",
        {
            "title": "Карта курса",
            "assignments": assignments,
            "lectures": lectures,
            "latest_submissions": latest_submissions,
            "statuses": STUDENT_STATUS,
            "accepted_count": accepted_count,
            "announcement_items": announcement_items,
            "last_answer": last_answer,
        },
        actor=actor,
    )


@router.get("/lectures")
def lectures(request: Request, actor: Actor = Depends(require_user)):
    conn = request.app.state.db_connect()
    try:
        if not actor.is_admin and not content_visible(conn, actor.group_id, utc_now()):
            return _exam_closed(request, actor)
        rows = conn.execute("SELECT * FROM lectures ORDER BY num, id").fetchall()
        visible = []
        for lecture in rows:
            if actor.is_admin:
                visible.append({"lecture": _lecture_template_data(lecture), "announce_only": False})
                continue
            if lecture_visible(conn, actor, lecture, utc_now()):
                visible.append({"lecture": _lecture_template_data(lecture), "announce_only": False})
            elif lecture["announce"] and lecture["planned_date"]:
                visible.append({"lecture": _lecture_template_data(lecture), "announce_only": True})
    finally:
        conn.close()
    return render(
        request,
        "lectures.html",
        {"title": "Лекции", "items": visible},
        actor=actor,
    )


def _lecture_access(request: Request, actor: Actor, slug: str):
    conn = request.app.state.db_connect()
    try:
        lecture = conn.execute(
            "SELECT * FROM lectures WHERE slug = ?", (slug,)
        ).fetchone()
        if not lecture:
            raise HTTPException(status_code=404)
        if actor.is_admin:
            return conn, lecture
        if exam_active(conn, actor.group_id, utc_now()):
            return conn, None
        if not lecture_visible(conn, actor, lecture, utc_now()):
            raise HTTPException(status_code=404)
        return conn, lecture
    except BaseException:
        conn.close()
        raise


def _exam_closed(request: Request, actor: Actor):
    conn = request.app.state.db_connect()
    try:
        exam = conn.execute("SELECT end_at FROM exam WHERE id = 1").fetchone()
    finally:
        conn.close()
    return render(
        request,
        "content_closed.html",
        {"title": "Материалы закрыты на время экзамена", "exam_end": exam["end_at"] if exam else None},
        actor=actor,
    )


def _lecture_template_data(lecture):
    values = dict(lecture)
    values["header_lines"] = json.loads(values["header_lines"] or "[]")
    return values


@router.get("/lectures/{slug}")
def lecture_page(request: Request, slug: str, actor: Actor = Depends(require_user)):
    conn, lecture = _lecture_access(request, actor, slug)
    if lecture is None:
        conn.close()
        return _exam_closed(request, actor)
    try:
        slides = conn.execute(
            "SELECT * FROM slides WHERE lecture_id = ? ORDER BY n", (lecture["id"],)
        ).fetchall()
        sections = conn.execute(
            "SELECT * FROM lecture_sections WHERE lecture_id = ? ORDER BY idx",
            (lecture["id"],),
        ).fetchall()
        if not slides:
            raise HTTPException(status_code=404)
        terms = glossary.visible_terms(conn, actor, lecture_slug=slug)
        return render(
            request,
            "lecture.html",
            {
                "title": lecture["title"],
                "lecture": _lecture_template_data(lecture),
                "slides": slides,
                "current": slides[0],
                "sections": sections,
                "terms": terms,
                "conspectus": False,
            },
            actor=actor,
        )
    finally:
        conn.close()


@router.get("/lectures/{slug}/conspectus")
def conspectus(request: Request, slug: str, actor: Actor = Depends(require_user)):
    conn, lecture = _lecture_access(request, actor, slug)
    if lecture is None:
        conn.close()
        return _exam_closed(request, actor)
    try:
        slides = conn.execute(
            "SELECT * FROM slides WHERE lecture_id = ? ORDER BY n", (lecture["id"],)
        ).fetchall()
        terms = glossary.visible_terms(conn, actor, lecture_slug=slug)
        return render(
            request,
            "lecture.html",
            {
                "title": lecture["title"],
                "lecture": _lecture_template_data(lecture),
                "slides": slides,
                "current": slides[0] if slides else None,
                "sections": [],
                "terms": terms,
                "conspectus": True,
            },
            actor=actor,
        )
    finally:
        conn.close()


@router.get("/lectures/{slug}/s/{number}")
def lecture_slide(
    request: Request, slug: str, number: int, actor: Actor = Depends(require_user)
):
    conn, lecture = _lecture_access(request, actor, slug)
    if lecture is None:
        conn.close()
        return _exam_closed(request, actor)
    try:
        slides = conn.execute(
            "SELECT * FROM slides WHERE lecture_id = ? ORDER BY n", (lecture["id"],)
        ).fetchall()
        current = next((slide for slide in slides if slide["n"] == number), None)
        if not current:
            raise HTTPException(status_code=404)
        sections = conn.execute(
            "SELECT * FROM lecture_sections WHERE lecture_id = ? ORDER BY idx",
            (lecture["id"],),
        ).fetchall()
        terms = glossary.visible_terms(conn, actor, lecture_slug=slug)
        return render(
            request,
            "lecture.html",
            {
                "title": lecture["title"],
                "lecture": _lecture_template_data(lecture),
                "slides": slides,
                "current": current,
                "sections": sections,
                "terms": terms,
                "conspectus": False,
            },
            actor=actor,
        )
    finally:
        conn.close()


@router.get("/l/{slug}/s/{number}.webp")
def slide_image(
    request: Request, slug: str, number: int, actor: Actor = Depends(require_user)
):
    return _lecture_file(request, actor, slug, number, "img")


@router.get("/l/{slug}/t/{number}.webp")
def slide_thumbnail(
    request: Request, slug: str, number: int, actor: Actor = Depends(require_user)
):
    return _lecture_file(request, actor, slug, number, "thumb")


def _lecture_file(
    request: Request, actor: Actor, slug: str, number: int, field: str
):
    conn, lecture = _lecture_access(request, actor, slug)
    try:
        if lecture is None:
            raise HTTPException(status_code=404)
        slide = conn.execute(
            "SELECT * FROM slides WHERE lecture_id = ? AND n = ?",
            (lecture["id"], number),
        ).fetchone()
        if not slide or not lecture["files_dir"]:
            raise HTTPException(status_code=404)
        settings = request.app.state.settings
        root = (settings.data_dir / "lectures").resolve()
        files_dir = Path(lecture["files_dir"])
        if not files_dir.is_absolute():
            files_dir = settings.base_dir / files_dir
        path = (files_dir / slide[field]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(
            path,
            media_type="image/webp",
            headers={"Cache-Control": "private, max-age=3600"},
        )
    finally:
        conn.close()


@router.get("/cabinet")
def cabinet(request: Request, actor: Actor = Depends(require_user)):
    if actor.is_admin:
        return RedirectResponse("/admin", status_code=303)
    conn = request.app.state.db_connect()
    try:
        data = cabinet_service.swot(conn, actor, request.app.state.settings)
    finally:
        conn.close()
    return render(
        request, "cabinet.html", {"title": "Личный кабинет", "swot": data}, actor=actor
    )


@router.get("/{section:path}")
def student_section(
    request: Request, section: str, actor: Actor = Depends(require_user)
):
    pages = {
        "announcements": ("Объявления", 5),
        "assignments": ("Задания", 4),
        "results": ("Мои результаты", 4),
        "messages": ("Связь с преподавателем", 5),
        "help": ("Помощь", 6),
        "help/upload": ("Загрузка файлов", 6),
    }
    if section not in pages:
        raise HTTPException(status_code=404)
    requested_view = request.query_params.get("view")
    if section.startswith("help"):
        if not can_view_help(actor, requested_view):
            raise HTTPException(status_code=404)
        view = help_view(actor, requested_view)
    else:
        view = "student"
    title, stage = pages[section]
    if section.startswith("help"):
        template = {
            ("help", "student"): "help_student.html",
            ("help", "teacher"): "help_teacher.html",
            ("help/upload", "student"): "upload_student.html",
            ("help/upload", "teacher"): "upload_teacher.html",
        }[(section, view)]
        return render(
            request,
            template,
            {"title": title, "help_view": view, "is_upload": section == "help/upload"},
            actor=actor,
        )
    return render(
        request,
        "section.html",
        {
            "title": title,
            "stage": stage,
            "help_view": view,
            "is_help": section.startswith("help"),
            "is_upload_help": section == "help/upload",
        },
        actor=actor,
    )
