from __future__ import annotations

import asyncio

import httpx

from app.db import connect, migrate
from app.services.balance import BalanceService, estimate_reserve
from app.worker import enqueue_job


def make_service(settings, handler):
    return BalanceService(settings, transport=httpx.MockTransport(handler))


def balance_response(value: float):
    return httpx.Response(200, json={"balance": value})


def test_balance_levels(settings):
    migrate(settings)
    assert round(estimate_reserve(settings), 2) == 14.24
    for balance, level in ((500, "ok"), (250, "low"), (50, "critical"), (10, "no_funds")):
        service = make_service(settings, lambda _request, value=balance: balance_response(value))
        report = asyncio.run(service.check())
        assert report.ok
        assert report.level == level
        assert report.paused == (level == "no_funds")


def test_balance_access_denied_does_not_stop_key(settings):
    migrate(settings)
    service = make_service(settings, lambda _request: httpx.Response(403))
    report = asyncio.run(service.check())
    assert report.level == "unknown"
    snapshot = service.snapshot()
    assert snapshot["state"]["balance_access"] == "denied"
    assert snapshot["state"]["gpt_stopped"] == 0


def test_rejected_key_pauses_queue(settings):
    migrate(settings)
    job_id = enqueue_job(settings, "grading")
    service = make_service(settings, lambda _request: httpx.Response(401))
    report = asyncio.run(service.check())
    assert report.paused
    conn = connect(settings)
    try:
        assert conn.execute("SELECT gpt_stopped FROM balance_state").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == "paused_no_funds"
    finally:
        conn.close()


def test_no_funds_pause_and_resume(settings):
    migrate(settings)
    job_id = enqueue_job(settings, "grading")
    current = {"balance": 10.0}
    service = make_service(
        settings, lambda _request: balance_response(current["balance"])
    )
    report = asyncio.run(service.check())
    assert report.paused
    conn = connect(settings)
    try:
        assert conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == "paused_no_funds"
    finally:
        conn.close()
    current["balance"] = 500.0
    report = asyncio.run(service.check())
    assert report.ok
    assert not report.paused
    assert "восстановлен" in report.error
    conn = connect(settings)
    try:
        assert conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == "queued"
    finally:
        conn.close()


def test_balance_alerts_are_queued_without_smtp(settings):
    migrate(settings)
    current = {"balance": 250.0}
    service = make_service(settings, lambda _request: balance_response(current["balance"]))
    asyncio.run(service.check())
    asyncio.run(service.check())
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'mail'").fetchone()[0] == 1
    finally:
        conn.close()
    current["balance"] = 10.0
    asyncio.run(service.check())
    current["balance"] = 500.0
    asyncio.run(service.check())
    conn = connect(settings)
    try:
        subjects = conn.execute("SELECT payload_json FROM jobs WHERE kind = 'mail'").fetchall()
        assert len(subjects) == 3
        assert "восстановлен" in subjects[-1]["payload_json"]
    finally:
        conn.close()


def test_full_snapshot_expenses_and_forecast(settings):
    migrate(settings)
    service = make_service(settings, lambda _request: balance_response(500))
    asyncio.run(service.check())
    conn = connect(settings)
    try:
        from app.db import utc_now_iso
        for cost in (0.39, 0.51):
            conn.execute(
                "INSERT INTO jobs(kind, priority, status, run_after, started_at, cost_est) "
                "VALUES ('grading', 3, 'done', ?, ?, ?)",
                (utc_now_iso(), utc_now_iso(), cost),
            )
    finally:
        conn.close()
    snapshot = service.snapshot()
    assert round(snapshot["expense_today"], 2) == 0.90
    assert snapshot["average_cost"] == 0.45
    assert snapshot["forecast_checks"] == 1111
    assert snapshot["history_points"]
    assert snapshot["billing_url"] == "https://proxyapi.ru"


def test_resume_if_possible(settings):
    migrate(settings)
    service = make_service(settings, lambda _request: balance_response(10))
    asyncio.run(service.check())
    conn = connect(settings)
    try:
        conn.execute("INSERT INTO balance_log(at, balance, source) VALUES ('2026-01-01T00:00:00Z', 500, 'manual')")
    finally:
        conn.close()
    assert service.resume_if_possible()
    snapshot = service.snapshot()
    assert snapshot["state"]["paused"] == 0
    assert snapshot["state"]["level"] == "ok"
