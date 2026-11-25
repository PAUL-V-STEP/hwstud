from __future__ import annotations

import asyncio
import csv
import io
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from starlette.responses import FileResponse, RedirectResponse, Response

from ..auth import Actor, check_csrf, flash
from ..db import parse_utc, transaction, utc_now, utc_now_iso
from ..services import exports, grading
from ..services import assignments as assignment_service
from ..services import cabinet as cabinet_service
from ..services.submissions import (
    STUDENT_STATUS,
    WorkValidationError,
    can_upload,
    prepare_file,
    teacher_decision,
)
from ..web import render
from .admin import require_admin
from .student import require_user

router = APIRouter()
MAX_UPLOAD = 10 * 1024 * 1024


def _latest(conn, student_id: int, assignment_id: int):
    return conn.execute(
        "SELECT * FROM submissions WHERE student_id = ? AND assignment_id = ? "
        "ORDER BY attempt DESC LIMIT 1",
        (student_id, assignment_id),
    ).fetchone()


def _quadrant(assignment, latest) -> str:
    if latest and latest["status"] in {"accepted", "exhausted"}:
        return "Q4"
    if latest and latest["status"] in {"queued", "checking", "awaiting_approval", "needs_review"}:
        return "Q3"
    due = parse_utc(assignment["due_at"])
    if due and (due - utc_now()).total_seconds() <= 7 * 86400:
        return "Q1"
    return "Q2"


@router.get("/assignments")
def assignments_page(request: Request, actor: Actor = Depends(require_user)):
    conn = request.app.state.db_connect()
    try:
        assignments = assignment_service.active_assignments(conn)
        latest = {
            assignment["id"]: _latest(conn, actor.student_id, assignment["id"])
            for assignment in assignments
        }
    finally:
        conn.close()
    cards = [
        {
            "assignment": assignment,
            "latest": latest[assignment["id"]],
            "quadrant": _quadrant(assignment, latest[assignment["id"]]),
            "can_upload": can_upload(latest[assignment["id"]]),
            "status_text": STUDENT_STATUS[
                latest[assignment["id"]]["status"] if latest[assignment["id"]] else "not_submitted"
            ],
        }
        for assignment in assignments
    ]
    return render(
        request,
        "assignments.html",
        {"title": "Задания", "cards": cards},
        actor=actor,
    )


@router.get("/assignments/{assignment_id}/export.pdf")
async def assignment_pdf(
    request: Request,
    assignment_id: int,
    actor: Actor = Depends(require_user),
):
    conn = request.app.state.db_connect()
    try:
        assignment = assignment_service.active_assignment(conn, assignment_id)
    finally:
        conn.close()
    if not assignment:
        raise HTTPException(status_code=404)
    content = await asyncio.to_thread(
        exports.export_assignment_pdf, request.app.state.settings, assignment
    )
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="assignment-{assignment["position"]}.pdf"'
        },
    )


async def _save_original(upload: UploadFile, target: Path) -> None:
    size = 0
    with target.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise WorkValidationError("Файл больше 10 МБ")
            output.write(chunk)
    if not size:
        raise WorkValidationError("Файл пуст")


@router.post("/assignments/{assignment_id}/submit")
async def submit_work(
    request: Request,
    assignment_id: int,
    work: UploadFile = File(...),
    csrf: str = Form(""),
    actor: Actor = Depends(require_user),
):
    check_csrf(request, csrf)
    if actor.is_admin:
        raise HTTPException(status_code=404)
    settings = request.app.state.settings
    conn = request.app.state.db_connect()
    try:
        assignment = assignment_service.active_assignment(conn, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404)
        latest = _latest(conn, actor.student_id, assignment_id)
        if not can_upload(latest):
            raise WorkValidationError("Сейчас новая загрузка недоступна")
    finally:
        conn.close()

    temporary = settings.data_dir / "tmp" / f"upload-{uuid.uuid4().hex}"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    original_target: Path | None = None
    prepared_target: Path | None = None
    semaphore = request.app.state.worker.semaphores["upload_parse"]
    try:
        await _save_original(work, temporary)
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=30)
        except TimeoutError as exc:
            raise WorkValidationError("Сервер занят, повторите через минуту") from exc
        try:
            prepared = await asyncio.to_thread(prepare_file, temporary)
        finally:
            semaphore.release()
        suffix = uuid.uuid4().hex
        original_target = settings.data_dir / "uploads" / f"{suffix}.{prepared.extension}"
        prepared_target = settings.data_dir / "tmp" / f"{suffix}.json"
        original_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(temporary), original_target)
        await asyncio.to_thread(prepared.write_json, prepared_target)

        conn = request.app.state.db_connect()
        try:
            with transaction(conn, immediate=True):
                latest = _latest(conn, actor.student_id, assignment_id)
                if not can_upload(latest):
                    raise WorkValidationError("Другая попытка уже была отправлена")
                attempt = latest["attempt"] + 1 if latest else 1
                due = parse_utc(assignment["due_at"])
                late = int(bool(due and utc_now() > due))
                original_relative = original_target.relative_to(settings.data_dir).as_posix()
                prepared_relative = prepared_target.relative_to(settings.data_dir).as_posix()
                cursor = conn.execute(
                    "INSERT INTO submissions(student_id, assignment_id, attempt, file, mime, "
                    "size, pages, chars, created_at, late, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')",
                    (
                        actor.student_id, assignment_id, attempt, original_relative,
                        prepared.mime, prepared.size, prepared.pages, prepared.chars,
                        utc_now_iso(), late,
                    ),
                )
                submission_id = int(cursor.lastrowid)
                conn.execute(
                    "INSERT INTO jobs(kind, priority, payload_json, run_after) "
                    "VALUES ('grading', 3, ?, ?)",
                    (
                        json.dumps(
                            {"submission_id": submission_id, "prepared": prepared_relative},
                            ensure_ascii=False,
                        ),
                        utc_now_iso(),
                    ),
                )
        finally:
            conn.close()
        flash(request, "success", "Работа загружена и поставлена в очередь проверки")
    except WorkValidationError as exc:
        if original_target:
            original_target.unlink(missing_ok=True)
        if prepared_target:
            prepared_target.unlink(missing_ok=True)
        flash(request, "error", str(exc))
    except Exception:
        if original_target:
            original_target.unlink(missing_ok=True)
        if prepared_target:
            prepared_target.unlink(missing_ok=True)
        flash(request, "error", "Файл не удалось прочитать. Проверьте его и повторите загрузку")
    finally:
        temporary.unlink(missing_ok=True)
        await work.close()
    return RedirectResponse("/assignments", status_code=303)


@router.get("/u/{submission_id}")
def submission_file(request: Request, submission_id: int, actor: Actor = Depends(require_user)):
    conn = request.app.state.db_connect()
    try:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if not row or (
            not actor.is_admin and row["student_id"] != actor.student_id
        ):
            raise HTTPException(status_code=404)
        root = (request.app.state.settings.data_dir / "uploads").resolve()
        path = (request.app.state.settings.data_dir / row["file"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(status_code=404)
    finally:
        conn.close()
    return FileResponse(
        path,
        media_type=row["mime"],
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"inline; filename=work-{submission_id}{path.suffix}",
        },
    )


@router.post("/assignments/review/{submission_id}")
def request_review(
    request: Request,
    submission_id: int,
    csrf: str = Form(""),
    actor: Actor = Depends(require_user),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if not row or row["student_id"] != actor.student_id:
            raise HTTPException(status_code=404)
        if row["status"] not in {"accepted", "revise", "exhausted"}:
            flash(request, "error", "Разбор можно запросить после решения преподавателя")
            return RedirectResponse("/results", status_code=303)
        existing = conn.execute(
            "SELECT 1 FROM review_requests WHERE submission_id = ? AND status = 'open'",
            (submission_id,),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO review_requests(submission_id, created_at) VALUES (?, ?)",
                (submission_id, utc_now_iso()),
            )
    finally:
        conn.close()
    flash(request, "success", "Запрос на разбор отправлен")
    return RedirectResponse("/results", status_code=303)


@router.get("/results")
def results_page(request: Request, variant: str = "1", actor: Actor = Depends(require_user)):
    conn = request.app.state.db_connect()
    try:
        assignments = assignment_service.active_assignments(conn)
        latest = {
            assignment["id"]: _latest(conn, actor.student_id, assignment["id"])
            for assignment in assignments
        }
        requests = conn.execute(
            "SELECT rr.*, s.assignment_id FROM review_requests rr "
            "JOIN submissions s ON s.id = rr.submission_id WHERE s.student_id = ?",
            (actor.student_id,),
        ).fetchall()
        assignment_ids = {str(assignment["id"]) for assignment in assignments}
        if variant != "sum" and variant not in assignment_ids:
            variant = "1"
        distribution_enabled = conn.execute(
            "SELECT value FROM settings WHERE key = 'distribution_enabled'"
        ).fetchone()["value"] == "1"
        scores, own = cabinet_service.decided_scores(conn, actor, variant)
        chart = cabinet_service.distribution_svg(
            scores, own, variant, assignment_count=len(assignments)
        ) if distribution_enabled else None
    finally:
        conn.close()
    return render(
        request,
        "results.html",
        {
            "title": "Мои результаты",
            "assignments": assignments,
            "latest": latest,
            "statuses": STUDENT_STATUS,
            "requests": {row["assignment_id"]: row for row in requests},
            "variant": variant,
            "chart": chart,
            "distribution_enabled": distribution_enabled,
            "distribution_count": len(scores),
        },
        actor=actor,
    )


@router.get("/admin/assignments")
def admin_assignments(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        assignments = assignment_service.active_assignments(conn)
    finally:
        conn.close()
    return render(
        request,
        "admin_assignments.html",
        {"title": "Задания", "assignments": assignments},
        actor=actor,
    )


@router.post("/admin/assignments/count")
def set_assignments_count(
    request: Request,
    count: int = Form(5),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        assignment_service.set_count(conn, count)
    except ValueError as exc:
        flash(request, "error", str(exc))
    else:
        flash(request, "success", f"Число домашних заданий: {count}")
    finally:
        conn.close()
    return RedirectResponse("/admin/assignments", status_code=303)


@router.post("/admin/assignments/{assignment_id}/move/{direction}")
def move_assignment(
    request: Request,
    assignment_id: int,
    direction: str,
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        moved = assignment_service.move(conn, assignment_id, direction)
    except ValueError as exc:
        flash(request, "error", str(exc))
    else:
        if moved:
            flash(request, "success", "Порядок заданий изменён")
    finally:
        conn.close()
    return RedirectResponse("/admin/assignments", status_code=303)


@router.post("/admin/assignments/{assignment_id}")
def edit_assignment(
    request: Request,
    assignment_id: int,
    title: str = Form(""),
    condition: str = Form(""),
    reference: str = Form(""),
    criteria: str = Form(""),
    typical_errors: str = Form(""),
    threshold: str = Form("0.7"),
    due_at: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    try:
        if not title.strip():
            raise ValueError("Введите название задания")
        assignment_text_length = sum(map(len, (condition, reference, criteria, typical_errors)))
        if assignment_text_length > 12_000:
            raise ValueError("Условие, эталон, критерии и ошибки суммарно превышают 12 000 знаков")
        threshold_value = float(threshold)
        if not 0 <= threshold_value <= 1:
            raise ValueError("Порог уверенности должен быть от 0 до 1")
        due_utc = None
        if due_at.strip():
            local = datetime.fromisoformat(due_at).replace(tzinfo=request.app.state.settings.timezone)
            due_utc = local.astimezone(UTC).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
        conn = request.app.state.db_connect()
        try:
            if not assignment_service.active_assignment(conn, assignment_id):
                raise ValueError("Задание не входит в текущий набор")
            instructions_row = conn.execute(
                "SELECT value FROM settings WHERE key = 'grading_instructions'"
            ).fetchone()
            instructions_length = len(instructions_row["value"]) if instructions_row else 0
            if assignment_text_length + instructions_length > 12_000:
                raise ValueError(
                    "Общие инструкции, условие, эталон, критерии и ошибки "
                    "суммарно превышают 12 000 знаков"
                )
            conn.execute(
                "UPDATE assignments SET title = ?, condition = ?, reference = ?, criteria = ?, "
                "typical_errors = ?, threshold = ?, due_at = ? WHERE id = ?",
                (
                    title.strip(), condition, reference, criteria, typical_errors,
                    threshold_value, due_utc, assignment_id,
                ),
            )
        finally:
            conn.close()
        flash(request, "success", "Задание обновлено для следующих проверок")
    except ValueError as exc:
        flash(request, "error", str(exc))
    return RedirectResponse("/admin/assignments", status_code=303)


@router.get("/admin/submissions")
def admin_submissions(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        submissions = conn.execute(
            "SELECT s.*, st.nick, g.name AS group_name, a.title, "
            "json_extract(s.similarity_json, '$.max_percent') AS similarity_percent "
            "FROM submissions s "
            "JOIN students st ON st.id = s.student_id JOIN groups g ON g.id = st.group_id "
            "JOIN assignments a ON a.id = s.assignment_id "
            "ORDER BY CASE WHEN s.status = 'needs_review' THEN 0 "
            "WHEN s.status = 'awaiting_approval' THEN 1 ELSE 2 END, s.created_at DESC"
        ).fetchall()
        review_requests = conn.execute(
            "SELECT rr.*, st.nick, a.title FROM review_requests rr "
            "JOIN submissions s ON s.id = rr.submission_id JOIN students st ON st.id = s.student_id "
            "JOIN assignments a ON a.id = s.assignment_id WHERE rr.status = 'open' ORDER BY rr.created_at"
        ).fetchall()
    finally:
        conn.close()
    return render(
        request,
        "admin_submissions.html",
        {
            "title": "Проверка работ",
            "submissions": submissions,
            "review_requests": review_requests,
        },
        actor=actor,
    )


def _admin_submission(conn, submission_id: int):
    return conn.execute(
        "SELECT s.*, st.nick, g.name AS group_name, a.title FROM submissions s "
        "JOIN students st ON st.id = s.student_id JOIN groups g ON g.id = st.group_id "
        "JOIN assignments a ON a.id = s.assignment_id WHERE s.id = ?",
        (submission_id,),
    ).fetchone()


def _similarity_for_teacher(conn, submission) -> dict | None:
    if not submission["similarity_json"]:
        return None
    similarity = json.loads(submission["similarity_json"])
    matches = similarity.get("matches", [])
    reference_ids = [int(match["reference_id"]) for match in matches]
    details = {}
    if reference_ids:
        placeholders = ",".join("?" for _ in reference_ids)
        rows = conn.execute(
            "SELECT s.id, s.attempt, st.nick, g.name AS group_name FROM submissions s "
            "JOIN students st ON st.id = s.student_id "
            "JOIN groups g ON g.id = st.group_id "
            f"WHERE s.id IN ({placeholders})",
            reference_ids,
        ).fetchall()
        details = {row["id"]: row for row in rows}
    enriched = []
    for match in matches:
        detail = details.get(int(match["reference_id"]))
        enriched.append({
            **match,
            "nick": detail["nick"] if detail else "Работа удалена",
            "group_name": detail["group_name"] if detail else "",
            "attempt": detail["attempt"] if detail else None,
            "exists": detail is not None,
        })
    similarity["matches"] = enriched
    return similarity


@router.get("/admin/submissions/{submission_id}")
def admin_submission(request: Request, submission_id: int, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        submission = _admin_submission(conn, submission_id)
        similarity = _similarity_for_teacher(conn, submission) if submission else None
        job = conn.execute(
            "SELECT * FROM jobs WHERE kind = 'grading' "
            "AND json_extract(payload_json, '$.submission_id') = ? ORDER BY id DESC LIMIT 1",
            (submission_id,),
        ).fetchone()
    finally:
        conn.close()
    if not submission:
        raise HTTPException(status_code=404)
    return render(
        request,
        "admin_submission.html",
        {
            "title": f"Работа {submission_id}", "submission": submission,
            "job": job, "similarity": similarity,
        },
        actor=actor,
    )


@router.post("/admin/submissions/{submission_id}/decision")
def decide_submission(
    request: Request,
    submission_id: int,
    verdict: str = Form(""),
    score: int = Form(0),
    comment: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        teacher_decision(conn, submission_id, verdict, score, comment)
    except ValueError as exc:
        flash(request, "error", str(exc))
    else:
        flash(request, "success", "Решение одобрено и открыто студенту")
    finally:
        conn.close()
    return RedirectResponse(f"/admin/submissions/{submission_id}", status_code=303)


@router.post("/admin/submissions/{submission_id}/recheck")
def recheck_submission(
    request: Request,
    submission_id: int,
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    try:
        job_id = grading.create_recheck_job(request.app.state.settings, submission_id)
        flash(request, "success", f"Создан новый платный запрос: задача {job_id}")
    except ValueError as exc:
        flash(request, "error", str(exc))
    return RedirectResponse(f"/admin/submissions/{submission_id}", status_code=303)


@router.post("/admin/review-requests/{request_id}")
def answer_review(
    request: Request,
    request_id: int,
    answer: str = Form(""),
    csrf: str = Form(""),
    actor: Actor = Depends(require_admin),
):
    check_csrf(request, csrf)
    conn = request.app.state.db_connect()
    try:
        conn.execute(
            "UPDATE review_requests SET status = 'answered', answer = ? WHERE id = ?",
            (answer.strip(), request_id),
        )
    finally:
        conn.close()
    flash(request, "success", "Разбор сохранён и доступен студенту")
    return RedirectResponse("/admin/submissions", status_code=303)


@router.get("/admin/results")
def admin_results(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        rows = _result_rows(conn)
    finally:
        conn.close()
    return render(
        request,
        "admin_results.html",
        {"title": "Результаты", "rows": rows},
        actor=actor,
    )


def _result_rows(conn):
    return conn.execute(
        "SELECT st.nick, g.name AS group_name, a.id AS assignment_id, a.title, "
        "s.status, s.score, s.attempt FROM submissions s "
        "JOIN students st ON st.id = s.student_id JOIN groups g ON g.id = st.group_id "
        "JOIN assignments a ON a.id = s.assignment_id "
        "WHERE s.id IN (SELECT MAX(id) FROM submissions GROUP BY student_id, assignment_id) "
        "ORDER BY g.id, st.nick, a.id"
    ).fetchall()


@router.get("/admin/results.csv")
def results_csv(request: Request, actor: Actor = Depends(require_admin)):
    conn = request.app.state.db_connect()
    try:
        rows = _result_rows(conn)
    finally:
        conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(("Группа", "Ник", "Задание", "Попытка", "Статус", "Баллы"))
    for row in rows:
        writer.writerow(
            (
                row["group_name"], row["nick"], row["title"], row["attempt"],
                row["status"], row["score"] if row["score"] is not None else "",
            )
        )
    return Response(
        output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="results.csv"'},
    )
