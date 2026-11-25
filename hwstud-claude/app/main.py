from __future__ import annotations

from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse

from .auth import RateLimiter
from .config import Settings, load_settings
from .db import connect, migrate
from .routes import admin, admin_lectures, assignments as assignment_routes, communication as communication_routes, course_admin, data as data_routes, public, references, registration as registration_routes, student
from .services.balance import BalanceService
from .services.access_cards import AccessCardStore
from .services.personal_keys import load_key_words
from .web import create_templates
from .worker import Worker


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        load_key_words()
        settings.ensure_directories()
        migrate(settings)
        if settings.enable_worker:
            await app.state.worker.start()
        try:
            yield
        finally:
            if settings.enable_worker:
                await app.state.worker.stop()

    app = FastAPI(
        title="hwstud.ru",
        version="2.1-stage-7",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.rate_limiter = RateLimiter()
    app.state.access_cards = AccessCardStore()
    app.state.db_connect = partial(connect, settings)
    app.state.templates = create_templates(settings.base_dir)
    app.state.balance = BalanceService(settings)
    app.state.worker = Worker(settings, balance=app.state.balance)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        max_age=90 * 24 * 60 * 60,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.add_middleware(GZipMiddleware, minimum_size=500)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
            "form-action 'self'"
        )
        return response

    @app.exception_handler(401)
    async def unauthenticated(request: Request, _exc):
        target = "/admin/login" if request.url.path.startswith("/admin") else "/login"
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(503)
    async def maintenance_unavailable(request: Request, _exc):
        return PlainTextResponse(
            "Идёт удаление данных студентов. Страница временно недоступна.",
            status_code=503,
        )

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok", "version": "2.1-stage-7"}

    app.mount(
        "/static",
        StaticFiles(directory=settings.base_dir / "static"),
        name="static",
    )
    app.include_router(public.router)
    app.include_router(admin.router)
    app.include_router(admin_lectures.router)
    app.include_router(course_admin.router)
    app.include_router(references.router)
    app.include_router(registration_routes.router)
    app.include_router(assignment_routes.router)
    app.include_router(communication_routes.router)
    app.include_router(data_routes.router)
    app.include_router(student.router)
    return app


app = create_app()
