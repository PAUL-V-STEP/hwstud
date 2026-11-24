from __future__ import annotations

import io
from pathlib import Path

import fitz
import pytest
from PIL import Image
from docx import Document

from app.db import connect
from app.services.submissions import (
    WorkValidationError,
    prepare_file,
    prepared_messages,
    teacher_decision,
)

from .conftest import csrf_from, login_admin, login_student


def image_file(path: Path, fmt: str = "PNG", size=(100, 80)):
    image = Image.new("RGB", size, "#2b59c3")
    image.save(path, fmt)
    return path


def test_txt_utf8_and_cp1251(tmp_path):
    path = tmp_path / "work.txt"
    path.write_text("Проверка текста", encoding="utf-8")
    prepared = prepare_file(path)
    assert prepared.chars == len("Проверка текста")
    path.write_bytes("Текст Windows".encode("cp1251"))
    assert prepare_file(path).parts[0]["text"] == "Текст Windows"


def test_pdf_text_and_scan_pages(tmp_path):
    path = tmp_path / "work.pdf"
    document = fitz.open()
    page = document.new_page(width=1000, height=700)
    page.insert_textbox(fitz.Rect(50, 50, 950, 500), "A" * 220, fontsize=12)
    document.new_page()
    document.save(path)
    document.close()
    prepared = prepare_file(path)
    assert prepared.pages == 2
    assert prepared.images == 1
    assert [part["type"] for part in prepared.parts] == ["text", "image"]


def test_pdf_rejects_six_scans(tmp_path):
    path = tmp_path / "scans.pdf"
    document = fitz.open()
    for _ in range(6):
        document.new_page()
    document.save(path)
    document.close()
    with pytest.raises(WorkValidationError, match="5 страниц"):
        prepare_file(path)


def test_docx_text_and_images_in_document_order(tmp_path):
    png = image_file(tmp_path / "one.png", "PNG")
    jpg = image_file(tmp_path / "two.jpg", "JPEG")
    document = Document()
    document.add_paragraph("Before")
    document.add_picture(str(png))
    document.add_paragraph("Middle")
    document.add_picture(str(jpg))
    document.add_paragraph("After")
    path = tmp_path / "work.docx"
    document.save(path)
    prepared = prepare_file(path)
    texts = [part["text"] for part in prepared.parts if part["type"] == "text" and part["text"].strip()]
    assert texts == ["Before", "Middle", "After"]
    assert prepared.images == 2
    assert [part["type"] for part in prepared.parts if part["type"] != "text" or part["text"].strip()] == [
        "text", "image", "text", "image", "text"
    ]
    prepared_path = tmp_path / "prepared.json"
    prepared.write_json(prepared_path)
    messages = prepared_messages(prepared_path)
    assert any(part["type"] == "image_url" for part in messages)


def test_docx_five_images_accepted_six_rejected(tmp_path):
    image = image_file(tmp_path / "pixel.png")
    for count in (5, 6):
        document = Document()
        for index in range(count):
            document.add_paragraph(f"Image {index}")
            document.add_picture(str(image))
        path = tmp_path / f"{count}.docx"
        document.save(path)
        if count == 5:
            prepared = prepare_file(path)
            assert prepared.images == 5
            assert prepared.chars > 0
        else:
            with pytest.raises(WorkValidationError, match="больше 5"):
                prepare_file(path)


def test_docx_only_screenshots_is_valid(tmp_path):
    image = image_file(tmp_path / "screen.png")
    document = Document()
    document.add_picture(str(image))
    path = tmp_path / "screens.docx"
    document.save(path)
    prepared = prepare_file(path)
    assert prepared.chars == 0
    assert prepared.images == 1


def test_docx_rejects_unsupported_embedded_image(tmp_path):
    bitmap = tmp_path / "image.bmp"
    Image.effect_noise((240, 180), 90).convert("RGB").save(bitmap, "BMP")
    document = Document()
    document.add_picture(str(bitmap))
    path = tmp_path / "bitmap.docx"
    document.save(path)
    with pytest.raises(WorkValidationError, match="JPEG или PNG"):
        prepare_file(path)


def test_text_limit(tmp_path):
    path = tmp_path / "large.txt"
    path.write_text("x" * 30_001, encoding="utf-8")
    with pytest.raises(WorkValidationError, match="30 000"):
        prepare_file(path)


def upload_txt(client, text="Решение задачи"):
    response = client.get("/assignments")
    response = client.post(
        "/assignments/1/submit",
        data={"csrf": csrf_from(response)},
        files={"work": ("work.txt", text.encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def test_upload_attempt_owner_and_approval_flow(client, settings):
    login_student(client, "work_student", 1)
    upload_txt(client)
    conn = connect(settings)
    try:
        submission = conn.execute("SELECT * FROM submissions").fetchone()
        assert submission["status"] == "queued"
        assert submission["attempt"] == 1
        job = conn.execute("SELECT * FROM jobs WHERE kind = 'grading'").fetchone()
        assert job["priority"] == 3
        file_id = submission["id"]
    finally:
        conn.close()
    assert client.get(f"/u/{file_id}").status_code == 200
    login_student(client, "other_student", 1)
    assert client.get(f"/u/{file_id}").status_code == 404
    login_student(client, "work_student", 1)
    conn = connect(settings)
    try:
        conn.execute("UPDATE submissions SET status = 'needs_review' WHERE id = ?", (file_id,))
        result = teacher_decision(conn, file_id, "revise", 4, "Нужны правки")
        assert result == "revise"
    finally:
        conn.close()
    upload_txt(client, "Исправленное решение")
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 2
        assert conn.execute("SELECT MAX(attempt) FROM submissions").fetchone()[0] == 2
        conn.execute("UPDATE submissions SET status = 'needs_review', attempt = 4 WHERE id = ?", (file_id,))
        assert teacher_decision(conn, file_id, "revise", 3, "Итог") == "exhausted"
    finally:
        conn.close()


def test_invalid_upload_does_not_consume_attempt(client, settings):
    login_student(client, "invalid_file", 1)
    response = client.get("/assignments")
    response = client.post(
        "/assignments/1/submit",
        data={"csrf": csrf_from(response)},
        files={"work": ("bad.zip", b"\x00\x00\xff", "application/zip")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
    finally:
        conn.close()


def test_busy_upload_does_not_create_attempt(client, settings):
    login_student(client, "busy_student", 1)
    class BusySemaphore:
        async def acquire(self):
            raise TimeoutError
        def release(self):
            pass
    client.app.state.worker.semaphores["upload_parse"] = BusySemaphore()
    response = client.get("/assignments")
    response = client.post(
        "/assignments/1/submit",
        data={"csrf": csrf_from(response)},
        files={"work": ("work.txt", b"Text", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
    finally:
        conn.close()


def test_parse_deadline_is_cooperative(tmp_path, monkeypatch):
    path = tmp_path / "deadline.pdf"
    document = fitz.open()
    document.new_page()
    document.save(path)
    document.close()
    import app.services.submissions as service
    monkeypatch.setattr(service.time, "monotonic", iter((0.0, 100.0)).__next__)
    with pytest.raises(WorkValidationError, match="отведённое время"):
        service.prepare_file(path)


def test_admin_results_csv_and_assignment_edit(client, settings):
    login_student(client, "csv_student", 1)
    upload_txt(client)
    login_admin(client)
    response = client.get("/admin/assignments")
    response = client.post(
        "/admin/assignments/1",
        data={
            "csrf": csrf_from(response), "title": "Первое задание",
            "condition": "Условие", "reference": "Эталон", "criteria": "Критерии",
            "typical_errors": "Ошибки", "threshold": "0.7", "due_at": "2026-12-01T18:30",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assignment = conn.execute("SELECT * FROM assignments WHERE id = 1").fetchone()
        assert assignment["title"] == "Первое задание"
        assert assignment["due_at"].endswith("Z")
    finally:
        conn.close()
    response = client.get("/admin/results.csv")
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "csv_student" in response.content.decode("utf-8-sig")


def test_student_exports_assignment_condition_without_teacher_fields(client, settings):
    conn = connect(settings)
    try:
        conn.execute(
            "UPDATE assignments SET title = ?, condition = ?, reference = ?, "
            "criteria = ?, typical_errors = ? WHERE id = 1",
            (
                "Анализ процесса",
                "Опишите входы, выходы и исполнителей процесса.",
                "Секретный эталон",
                "Скрытые критерии",
                "Скрытые ошибки",
            ),
        )
    finally:
        conn.close()
    login_student(client, "pdf_student", 1)
    page = client.get("/assignments")
    assert '/assignments/1/export.pdf' in page.text
    response = client.get("/assignments/1/export.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"] == 'attachment; filename="assignment-1.pdf"'
    assert response.content.startswith(b"%PDF")
    with fitz.open(stream=response.content, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
    assert "Анализ процесса" in text
    assert "Опишите входы, выходы и исполнителей процесса." in text
    assert "Секретный эталон" not in text
    assert "Скрытые критерии" not in text
    assert "Скрытые ошибки" not in text


def test_fifth_course_assignment_full_flow(client, settings):
    login_admin(client)
    response = client.get("/admin/assignments")
    assert "Общее задание по всему курсу" in response.text
    response = client.post(
        "/admin/assignments/5",
        data={
            "csrf": csrf_from(response),
            "title": "Итоговое задание по курсу",
            "condition": "Примените все изученные методы к одному процессу.",
            "reference": "Эталон итогового задания",
            "criteria": "Критерии итогового задания",
            "typical_errors": "Типичные ошибки",
            "threshold": "0.7",
            "due_at": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    login_student(client, "fifth_assignment", 1)
    dashboard = client.get("/")
    assert "0 / 5 принято" in dashboard.text
    page = client.get("/assignments")
    assert "Итоговое задание по курсу" in page.text
    assert 'action="/assignments/5/submit"' in page.text
    assert 'href="/assignments/5/export.pdf"' in page.text
    response = client.post(
        "/assignments/5/submit",
        data={"csrf": csrf_from(page)},
        files={"work": ("course.txt", "Итоговое решение".encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        submission = conn.execute(
            "SELECT * FROM submissions WHERE assignment_id = 5"
        ).fetchone()
        assert submission["status"] == "queued"
        assert submission["attempt"] == 1
    finally:
        conn.close()
    results = client.get("/results?variant=5")
    assert results.status_code == 200
    assert "Итоговое задание по курсу" in results.text
    assert "На проверке" in results.text
    assert client.get("/assignments/5/export.pdf").content.startswith(b"%PDF")
