from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import timedelta
from typing import Any

from .config import Settings
from .db import connect, transaction, utc_now, utc_now_iso
from .services import lecture_import
from .services import communication
from .services import backup
from .services import grading
from .services.balance import BalanceService
from .services.mailer import Mailer
from .services.proxyapi import Outcome, ProxyAPIClient
from .services.schemas import ABBREVIATION_RESPONSE, SOURCE_META_RESPONSE

JOB_PRIORITIES = {
    "mail": 1,
    "lecture_import": 2,
    "delete_students": 2,
    "grading": 3,
    "parse_abbr": 4,
    "abbr_guess": 4,
    "source_meta": 5,
    "export": 6,
    "delete_temp": 6,
}
GPT_KINDS = {"grading", "parse_abbr", "abbr_guess", "source_meta"}


class PauseJob(Exception):
    pass


class DeferJob(Exception):
    pass


class FailJob(Exception):
    pass


def enqueue_job(
    settings: Settings,
    kind: str,
    *,
    payload: dict[str, Any] | None = None,
    priority: int | None = None,
    run_after: str | None = None,
) -> int:
    if kind not in JOB_PRIORITIES:
        raise ValueError(f"Неизвестный вид задачи: {kind}")
    conn = connect(settings)
    try:
        with transaction(conn, immediate=True):
            cursor = conn.execute(
                "INSERT INTO jobs(kind, priority, payload_json, run_after) "
                "VALUES (?, ?, ?, ?)",
                (
                    kind,
                    priority or JOB_PRIORITIES[kind],
                    json.dumps(payload or {}, ensure_ascii=False),
                    run_after or utc_now_iso(),
                ),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def claim_next_job(settings: Settings) -> sqlite3.Row | None:
    conn = connect(settings)
    try:
        with transaction(conn, immediate=True):
            balance = conn.execute(
                "SELECT paused, gpt_stopped FROM balance_state WHERE id = 1"
            ).fetchone()
            confirmed = conn.execute(
                "SELECT value FROM settings WHERE key = 'models_confirmed'"
            ).fetchone()
            gpt_blocked = (
                balance["paused"]
                or balance["gpt_stopped"]
                or not confirmed
                or confirmed["value"] != "1"
            )
            exclusion = ""
            parameters: list[Any] = [utc_now_iso(), utc_now_iso()]
            if gpt_blocked:
                placeholders = ",".join("?" for _ in GPT_KINDS)
                exclusion = f"AND kind NOT IN ({placeholders})"
                parameters.extend(GPT_KINDS)
            row = conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ? "
                "WHERE id = ("
                "SELECT id FROM jobs WHERE status = 'queued' AND run_after <= ? "
                + exclusion
                + " ORDER BY priority, id LIMIT 1"
                + ") RETURNING *",
                parameters,
            ).fetchone()
            return row
    finally:
        conn.close()


def claim_probe_job(settings: Settings) -> sqlite3.Row | None:
    conn = connect(settings)
    try:
        with transaction(conn, immediate=True):
            state = conn.execute(
                "SELECT gpt_stopped FROM balance_state WHERE id = 1"
            ).fetchone()
            confirmed = conn.execute(
                "SELECT value FROM settings WHERE key = 'models_confirmed'"
            ).fetchone()
            if state["gpt_stopped"] or not confirmed or confirmed["value"] != "1":
                return None
            placeholders = ",".join("?" for _ in GPT_KINDS)
            parameters: list[Any] = [utc_now_iso(), *GPT_KINDS]
            return conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ("
                f"SELECT id FROM jobs WHERE status = 'paused_no_funds' "
                f"AND kind IN ({placeholders}) ORDER BY priority, id LIMIT 1"
                ") RETURNING *",
                parameters,
            ).fetchone()
    finally:
        conn.close()


def recover_jobs(settings: Settings) -> None:
    conn = connect(settings)
    try:
        with transaction(conn, immediate=True):
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = 'running'"
            ).fetchall()
            for row in rows:
                tries = row["tries"] + 1
                if row["kind"] == "grading" and row["request_sent_at"]:
                    conn.execute(
                        "UPDATE jobs SET status = 'failed', tries = ?, "
                        "error = 'Ответ ИИ потерян при перезапуске сервера' WHERE id = ?",
                        (tries, row["id"]),
                    )
                    payload = json.loads(row["payload_json"])
                    if payload.get("submission_id"):
                        conn.execute(
                            "UPDATE submissions SET status = 'needs_review' WHERE id = ?",
                            (payload["submission_id"],),
                        )
                elif tries >= 5:
                    conn.execute(
                        "UPDATE jobs SET status = 'failed', tries = ?, "
                        "error = 'Превышено число запусков' WHERE id = ?",
                        (tries, row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET status = 'queued', tries = ?, started_at = NULL "
                        "WHERE id = ?",
                        (tries, row["id"]),
                    )
    finally:
        conn.close()


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        mailer: Mailer | None = None,
        balance: BalanceService | None = None,
        proxy_client: ProxyAPIClient | None = None,
    ) -> None:
        self.settings = settings
        self.mailer = mailer or Mailer(settings)
        self.balance = balance or BalanceService(settings)
        self.proxy_client = proxy_client or ProxyAPIClient(settings)
        self.semaphores = {
            "heavy": asyncio.Semaphore(1),
            "upload_parse": asyncio.Semaphore(2),
            "gpt": asyncio.Semaphore(2),
            "mail": asyncio.Semaphore(1),
        }
        self._stop_event = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._running: set[asyncio.Task[Any]] = set()
        self._last_balance_check = time.monotonic()
        self._last_probe_check = time.monotonic()
        self._last_communication_check = 0.0
        self._last_maintenance_check = 0.0

    async def start(self) -> None:
        if self._loop_task:
            return
        self._stop_event.clear()
        lecture_import.recover_saving_imports(self.settings)
        lecture_import.expire_ready_imports(self.settings)
        recover_jobs(self.settings)
        backup.resume_student_deletion(self.settings)
        self._loop_task = asyncio.create_task(self._loop(), name="hwstud-worker")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)
            self._running.clear()

    async def run_probe(self) -> bool:
        job = claim_probe_job(self.settings)
        if not job:
            return False
        await self._run_job(dict(job))
        conn = connect(self.settings)
        try:
            done = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job["id"],)
            ).fetchone()[0] == "done"
        finally:
            conn.close()
        if done:
            self.balance.force_resume()
        return done

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            finished = {task for task in self._running if task.done()}
            for task in finished:
                task.exception() if not task.cancelled() else None
            self._running.difference_update(finished)
            while len(self._running) < 10:
                job = claim_next_job(self.settings)
                if not job:
                    break
                task = asyncio.create_task(self._run_job(dict(job)))
                self._running.add(task)
            self._schedule_balance_if_due()
            if time.monotonic() - self._last_communication_check >= 60:
                self._last_communication_check = time.monotonic()
                communication.enqueue_hourly_digest_if_due(self.settings)
            if time.monotonic() - self._last_maintenance_check >= 60:
                self._last_maintenance_check = time.monotonic()
                maintenance_task = asyncio.create_task(self._run_maintenance_due())
                self._running.add(maintenance_task)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=2)
            except TimeoutError:
                pass

    async def _run_maintenance_due(self) -> None:
        async with self.semaphores["heavy"]:
            await asyncio.to_thread(backup.run_due_maintenance, self.settings)

    def _schedule_balance_if_due(self) -> None:
        if not self.settings.proxyapi_key:
            return
        snapshot = self.balance.snapshot()
        if snapshot["state"]["paused"] and snapshot["state"]["balance_access"] == "denied":
            if time.monotonic() - self._last_probe_check >= 900:
                self._last_probe_check = time.monotonic()
                task = asyncio.create_task(self.run_probe())
                self._running.add(task)
            return
        interval = 300 if snapshot["state"]["paused"] else 1800
        if time.monotonic() - self._last_balance_check < interval:
            return
        self._last_balance_check = time.monotonic()
        task = asyncio.create_task(self.balance.check(source="poll"))
        self._running.add(task)

    def _semaphore_for(self, kind: str) -> asyncio.Semaphore:
        if kind == "mail":
            return self.semaphores["mail"]
        if kind in GPT_KINDS:
            return self.semaphores["gpt"]
        if kind == "lecture_import":
            return self.semaphores["heavy"]
        return self.semaphores["heavy"]

    async def _run_job(self, job: dict[str, Any]) -> None:
        async with self._semaphore_for(job["kind"]):
            try:
                payload = json.loads(job["payload_json"])
                result = None
                if job["kind"] == "mail":
                    await asyncio.to_thread(
                        self.mailer.send,
                        subject=payload["subject"],
                        body=payload["body"],
                    )
                    communication.mark_mailed(self.settings, payload)
                elif job["kind"] == "lecture_import":
                    await asyncio.to_thread(
                        lecture_import.process_import,
                        self.settings,
                        int(payload["import_id"]),
                    )
                elif job["kind"] in {"parse_abbr", "abbr_guess", "source_meta", "grading"}:
                    if job["kind"] == "grading":
                        result = await grading.grade_job(
                            self.settings, job, self.proxy_client, self.balance
                        )
                    else:
                        result = await self._run_gpt(job, payload)
                elif job["kind"] == "export":
                    archive = await asyncio.to_thread(
                        backup.create_works_archive, self.settings
                    )
                    result = {"archive": archive.name}
                elif job["kind"] == "delete_temp":
                    await asyncio.to_thread(
                        backup.delete_temporary_file, self.settings, payload["path"]
                    )
                elif job["kind"] == "delete_students":
                    await asyncio.to_thread(backup.delete_student_data, self.settings)
                else:
                    raise RuntimeError(f"Неизвестный вид задачи: {job['kind']}")
            except PauseJob as exc:
                self._pause_current_job(int(job["id"]), str(exc))
            except DeferJob as exc:
                self._defer_current_job(int(job["id"]), str(exc))
            except FailJob as exc:
                self._fail_permanently(int(job["id"]), str(exc))
                if job["kind"] in {"parse_abbr", "abbr_guess", "source_meta"}:
                    lecture_import.maybe_finish_import(
                        self.settings, int(payload["import_id"])
                    )
            except Exception as exc:
                self._fail_job(job, str(exc))
            else:
                self._complete_job(job, result)
                if job["kind"] in {"parse_abbr", "abbr_guess", "source_meta"}:
                    lecture_import.maybe_finish_import(
                        self.settings, int(payload["import_id"])
                    )
                if job["kind"] in GPT_KINDS:
                    await self._after_gpt_request()

    async def _after_gpt_request(self) -> None:
        if not self.settings.proxyapi_key:
            return
        conn = connect(self.settings)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE request_sent_at IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        if count and count % 10 == 0:
            await self.balance.check(source="poll")

    async def _run_gpt(
        self, job: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        if not self._models_confirmed():
            raise DeferJob("Модели ещё не подтверждены")
        if job["kind"] == "parse_abbr":
            system = (
                "Разбери строки сокращений. Верни JSON с items: списком объектов "
                "term, expansion, english. Сохраняй исходный термин и не выдумывай данные."
            )
            user_content = json.dumps(payload["lines"], ensure_ascii=False)
        elif job["kind"] == "abbr_guess":
            system = (
                "По контексту предложи расшифровку сокращения. Верни JSON с items: "
                "term, expansion, english. Если уверенности нет, expansion оставь пустым."
            )
            user_content = json.dumps(payload["items"], ensure_ascii=False)
        else:
            system = (
                "По автору, названию и контексту лекции определи тип источника "
                "(article, documentation, book, manual) и краткую аннотацию на русском "
                "в 1–2 предложения. Не открывай ссылку и не выдумывай содержимое. "
                "Верни JSON с полями type и annotation."
            )
            user_content = json.dumps(
                {
                    "author": payload["author"],
                    "title": payload["title"],
                    "url": payload["url"],
                    "context": payload["context"],
                },
                ensure_ascii=False,
            )
        model = self.settings.model_aux
        conn = connect(self.settings)
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE jobs SET request_sent_at = ?, model = ? WHERE id = ?",
                    (utc_now_iso(), model, job["id"]),
                )
        finally:
            conn.close()
        result = await self.proxy_client.chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            max_completion_tokens=2000,
            reasoning_effort="low",
            response_format=(
                SOURCE_META_RESPONSE
                if job["kind"] == "source_meta"
                else ABBREVIATION_RESPONSE
            ),
        )
        if result.outcome == Outcome.NO_FUNDS:
            self.balance.pause("ProxyAPI ответил 402")
            raise PauseJob("Нет средств на ProxyAPI")
        if result.outcome == Outcome.KEY_REJECTED:
            self.balance.reject_key(result.error)
            raise PauseJob("Ключ ProxyAPI отклонён")
        if not result.ok:
            raise FailJob(result.error or result.outcome.value)
        if job["kind"] == "source_meta":
            if not isinstance(result.content.get("type"), str) or not isinstance(
                result.content.get("annotation"), str
            ):
                raise FailJob("GPT не вернул type и annotation источника")
            return result.content
        items = result.content.get("items") if result.content else None
        if not isinstance(items, list) or not all(
            isinstance(item, dict) and isinstance(item.get("term"), str)
            for item in items
        ):
            raise FailJob("GPT не вернул корректный список items")
        job["model"] = result.model or model
        job["tokens_in"] = result.tokens_in
        job["tokens_out"] = result.tokens_out
        job["cost_est"] = grading.estimate_aux_cost(
            self.settings, result.tokens_in, result.tokens_out
        )
        return result.content

    def _models_confirmed(self) -> bool:
        conn = connect(self.settings)
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'models_confirmed'"
            ).fetchone()
            return bool(row and row["value"] == "1")
        finally:
            conn.close()

    def _complete_job(self, job: dict[str, Any], result: dict[str, Any] | None) -> None:
        conn = connect(self.settings)
        try:
            with transaction(conn):
                payload = json.loads(job["payload_json"])
                if result is not None:
                    payload["result"] = result
                conn.execute(
                    "UPDATE jobs SET status = 'done', error = NULL, payload_json = ?, "
                    "model = ?, tokens_in = ?, tokens_out = ?, cost_est = ? "
                    "WHERE id = ? AND status = 'running'",
                    (
                        json.dumps(payload, ensure_ascii=False),
                        job.get("model"),
                        job.get("tokens_in"),
                        job.get("tokens_out"),
                        job.get("cost_est"),
                        job["id"],
                    ),
                )
        finally:
            conn.close()

    def _pause_current_job(self, job_id: int, error: str) -> None:
        conn = connect(self.settings)
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE jobs SET status = 'paused_no_funds', started_at = NULL, "
                    "error = ? WHERE id = ?",
                    (error, job_id),
                )
        finally:
            conn.close()

    def _defer_current_job(self, job_id: int, error: str) -> None:
        run_after = (utc_now() + timedelta(seconds=60)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        conn = connect(self.settings)
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE jobs SET status = 'queued', run_after = ?, started_at = NULL, "
                    "error = ? WHERE id = ?",
                    (run_after, error, job_id),
                )
        finally:
            conn.close()

    def _fail_permanently(self, job_id: int, error: str) -> None:
        conn = connect(self.settings)
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                    (error[:1000], job_id),
                )
        finally:
            conn.close()

    def _fail_job(self, job: dict[str, Any], error: str) -> None:
        conn = connect(self.settings)
        try:
            with transaction(conn):
                tries = int(job["tries"]) + 1
                if job["kind"] == "mail" and tries <= 3:
                    delays = (300, 900, 3600)
                    run_after = (utc_now() + timedelta(seconds=delays[tries - 1])).isoformat(
                        timespec="seconds"
                    ).replace("+00:00", "Z")
                    conn.execute(
                        "UPDATE jobs SET status = 'queued', tries = ?, run_after = ?, "
                        "started_at = NULL, error = ? WHERE id = ?",
                        (tries, run_after, error[:1000], job["id"]),
                    )
                elif tries >= 5 or job["kind"] == "mail":
                    conn.execute(
                        "UPDATE jobs SET status = 'failed', tries = ?, error = ? WHERE id = ?",
                        (tries, error[:1000], job["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET status = 'queued', tries = ?, started_at = NULL, "
                        "error = ? WHERE id = ?",
                        (tries, error[:1000], job["id"]),
                    )
        finally:
            conn.close()
