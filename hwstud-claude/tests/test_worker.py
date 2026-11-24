from __future__ import annotations

import asyncio

from app.db import connect, migrate, utc_now_iso
from app.worker import Worker, claim_next_job, enqueue_job, recover_jobs


class FakeMailer:
    def __init__(self, fail: bool = False):
        self.messages = []
        self.fail = fail

    def send(self, *, subject: str, body: str) -> None:
        if self.fail:
            raise RuntimeError("SMTP stub failed")
        self.messages.append((subject, body))


def test_atomic_claim_and_fake_mail_completion(settings):
    migrate(settings)
    job_id = enqueue_job(
        settings,
        "mail",
        payload={"subject": "Проба", "body": "Текст"},
    )
    row = claim_next_job(settings)
    assert row["id"] == job_id
    assert row["status"] == "running"
    assert claim_next_job(settings) is None
    mailer = FakeMailer()
    worker = Worker(settings, mailer=mailer)
    asyncio.run(worker._run_job(dict(row)))
    assert mailer.messages == [("Проба", "Текст")]
    conn = connect(settings)
    try:
        assert conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == "done"
    finally:
        conn.close()


def test_mail_failure_is_requeued_without_smtp(settings):
    migrate(settings)
    job_id = enqueue_job(
        settings,
        "mail",
        payload={"subject": "Проба", "body": "Текст"},
    )
    row = claim_next_job(settings)
    worker = Worker(settings, mailer=FakeMailer(fail=True))
    asyncio.run(worker._run_job(dict(row)))
    conn = connect(settings)
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert job["status"] == "queued"
        assert job["tries"] == 1
        assert job["run_after"] > utc_now_iso()
        assert "SMTP stub failed" in job["error"]
    finally:
        conn.close()


def test_recovery_does_not_repeat_sent_grading_request(settings):
    migrate(settings)
    conn = connect(settings)
    try:
        conn.execute(
            "INSERT INTO jobs(kind, priority, status, run_after, request_sent_at) "
            "VALUES ('grading', 3, 'running', ?, ?)",
            (utc_now_iso(), utc_now_iso()),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO jobs(kind, priority, status, run_after) "
            "VALUES ('mail', 1, 'running', ?)",
            (utc_now_iso(),),
        )
        mail_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()
    recover_jobs(settings)
    conn = connect(settings)
    try:
        grading = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        mail = conn.execute("SELECT * FROM jobs WHERE id = ?", (mail_id,)).fetchone()
        assert grading["status"] == "failed"
        assert "потерян" in grading["error"]
        assert mail["status"] == "queued"
    finally:
        conn.close()


def test_worker_semaphore_limits(settings):
    worker = Worker(settings, mailer=FakeMailer())
    assert worker.semaphores["heavy"]._value == 1
    assert worker.semaphores["upload_parse"]._value == 2
    assert worker.semaphores["gpt"]._value == 2
    assert worker.semaphores["mail"]._value == 1
