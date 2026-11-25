from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .auth import Actor, current_actor, ensure_csrf, pop_flashes
from .config import get_timezone
from .db import parse_utc
from .services import communication, glossary, literature, registration


def create_templates(base_dir: Path) -> Jinja2Templates:
    templates = Jinja2Templates(directory=base_dir / "app" / "templates")

    def format_moscow(value: str | None) -> str:
        moment = parse_utc(value)
        if not moment:
            return "—"
        from zoneinfo import ZoneInfo

        return moment.astimezone(get_timezone("Europe/Moscow")).strftime(
            "%d.%m.%Y %H:%M МСК"
        )

    def format_moscow_input(value: str | None) -> str:
        moment = parse_utc(value)
        if not moment:
            return ""
        from zoneinfo import ZoneInfo

        return moment.astimezone(get_timezone("Europe/Moscow")).strftime("%Y-%m-%dT%H:%M")

    templates.env.filters["moscow"] = format_moscow
    templates.env.filters["moscow_input"] = format_moscow_input
    templates.env.filters["terms"] = glossary.highlight_terms
    templates.env.filters["citations"] = literature.citation_links
    return templates


def _back_navigation(request: Request, actor: Actor | None, values: dict[str, Any]):
    path = request.url.path
    if (actor and path == "/") or (actor and actor.is_admin and path == "/admin"):
        return None
    if path == "/admin/settings/password" and request.session.get("must_change_password"):
        return None
    if path == "/admin/data/deletion":
        state = values.get("state")
        if state and state["started_at"]:
            return None
    if path == "/about":
        return ("/" if actor else "/login", "Карта курса" if actor else "Ко входу")
    if path == "/admin/login":
        return "/login", "Ко входу студента"
    if path == "/login":
        return None
    if path == "/glossary" or path == "/literature":
        slug = values.get("lecture")
        if slug:
            return f"/lectures/{slug}", "Вернуться к лекции"
        return "/lectures", "К списку лекций"
    if path == "/announcements" or path.startswith("/announcements/"):
        return ("/announcements", "К объявлениям") if path != "/announcements" else ("/", "Карта курса")
    if path in {"/lectures", "/assignments", "/results", "/messages", "/cabinet", "/help", "/help/upload"}:
        return ("/admin", "Обзор") if actor and actor.is_admin else ("/", "Карта курса")
    if path.startswith("/lectures/"):
        slug = path.split("/")[2]
        if path.endswith("/conspectus"):
            return f"/lectures/{slug}", "К просмотру лекции"
        return "/lectures", "К списку лекций"
    if path.startswith("/admin/messages/"):
        return "/admin/messages", "К списку веток"
    if path.startswith("/admin/settings/"):
        return "/admin/settings", "К настройкам"
    if path.startswith("/admin/submissions/"):
        return "/admin/submissions", "К очереди проверки"
    if path in {"/admin/data/delete", "/admin/data/deletion"}:
        return "/admin/data", "К данным курса"
    if path.startswith("/admin/lectures/") or path.startswith("/admin/imports/"):
        return "/admin/lectures", "К списку лекций"
    if path.startswith("/admin/students/") or path.startswith("/admin/groups/"):
        return "/admin/groups", "К группам и никам"
    if path in {
        "/admin/lectures", "/admin/announcements", "/admin/assignments", "/admin/submissions",
        "/admin/results", "/admin/glossary", "/admin/literature", "/admin/settings",
        "/admin/exam", "/admin/groups", "/admin/about", "/admin/data", "/admin/messages",
    }:
        return "/admin", "Обзор"
    if actor:
        return ("/admin", "Обзор") if actor.is_admin else ("/", "Карта курса")
    return "/login", "Ко входу"


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    actor: Actor | None = None,
) -> Any:
    values = dict(context or {})
    values.update(
        {
            "request": request,
            "actor": actor if actor is not None else current_actor(request),
            "csrf": ensure_csrf(request),
            "flashes": pop_flashes(request),
            "app_base_url": request.app.state.settings.app_base_url,
        }
    )
    back = _back_navigation(request, values["actor"], values)
    if back and "back_href" not in values:
        values["back_href"], values["back_label"] = back
    if values["actor"] and not values["actor"].is_admin:
        conn = request.app.state.db_connect()
        try:
            values["notifications"] = {
                "announcements": communication.unread_announcements_count(conn, values["actor"]),
                "replies": communication.new_teacher_replies_count(conn, values["actor"].student_id),
            }
        finally:
            conn.close()
    else:
        values["notifications"] = {"announcements": 0, "replies": 0}
    if values["actor"] and values["actor"].is_admin:
        conn = request.app.state.db_connect()
        try:
            values["notifications"]["teacher_unread"] = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE author = 'student' "
                "AND teacher_read_at IS NULL"
            ).fetchone()[0]
            registration_state = registration.status(conn)
            seconds = max(
                (
                    item["seconds_left"]
                    for item in registration_state["windows"]
                ),
                default=0,
            )
            values["notifications"]["registration_clock"] = (
                f"{seconds // 60:02d}:{seconds % 60:02d}" if seconds else ""
            )
        finally:
            conn.close()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=name,
        context=values,
        status_code=status_code,
    )
