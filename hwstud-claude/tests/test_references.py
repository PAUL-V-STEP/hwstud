from __future__ import annotations

import asyncio
import json

from app.db import connect
from app.services import glossary, lecture_import, literature
from app.services.lecture_store import save_import
from app.services.proxyapi import Outcome, ProxyResult
from app.worker import Worker, claim_next_job

from .conftest import csrf_from, login_admin, login_student
from .lecture_helpers import create_lecture_import, import_row


def create_reference_import(settings):
    return create_lecture_import(
        settings,
        titles=("Process Analysis", "Литература"),
        notes=(
            "Overview\nBPMN describes processes [1].\n"
            "[1] Fowler. Analysis Patterns. https://example.test/patterns\n"
            "Сокращения: BPMN — Business Process Model and Notation, нотация процессов",
            "Литература\n[2] Gamma. Design Patterns. https://example.test/design",
        ),
    )


class FakeSourceClient:
    async def chat_completion(self, **kwargs):
        user = json.loads(kwargs["messages"][1]["content"])
        return ProxyResult(
            Outcome.SUCCESS,
            status_code=200,
            content={
                "type": "book" if "Design" in user.get("title", "") else "documentation",
                "annotation": "Краткая аннотация источника для проверки.",
            },
            model="aux-test",
            tokens_in=20,
            tokens_out=10,
        )


def complete_source_jobs(settings):
    conn = connect(settings)
    try:
        conn.execute("UPDATE settings SET value = '1' WHERE key = 'models_confirmed'")
    finally:
        conn.close()
    worker = Worker(settings, proxy_client=FakeSourceClient())
    while job := claim_next_job(settings):
        assert job["kind"] == "source_meta"
        asyncio.run(worker._run_job(dict(job)))


def confirm_all(client, import_id: int):
    response = client.get(f"/admin/lectures/import/{import_id}")
    assert response.status_code == 200
    return client.post(
        f"/admin/lectures/import/{import_id}/confirm",
        data={
            "csrf": csrf_from(response),
            "terms": ["0"],
            "variant_0": "0",
            "expansion_0": "Business Process Model and Notation",
            "english_0": "Business Process Model and Notation",
            "sources": ["0", "1"],
            "source_title_0": "Analysis Patterns",
            "source_author_0": "Fowler",
            "source_title_1": "Design Patterns",
            "source_author_1": "Gamma",
        },
        follow_redirects=False,
    )


def saved_references(settings, client):
    import_id = create_reference_import(settings)
    lecture_import.process_import(settings, import_id)
    assert import_row(settings, import_id)["status"] == "waiting_gpt"
    complete_source_jobs(settings)
    assert import_row(settings, import_id)["status"] == "ready"
    login_admin(client)
    response = confirm_all(client, import_id)
    assert response.status_code == 303
    return save_import(settings, import_id, "Process Analysis")


def test_source_parsing_and_metadata_stub(settings):
    import_id = create_reference_import(settings)
    lecture_import.process_import(settings, import_id)
    data = json.loads(import_row(settings, import_id)["report_json"])
    assert len(data["sources"]) == 2
    assert data["sources"][0]["author"] == "Fowler"
    assert data["sources"][1]["local_number"] == 2
    assert data["citations"] == [{"slide_n": 1, "local_number": 1}]
    complete_source_jobs(settings)
    review = literature.build_source_review(settings, import_id)
    assert review[0]["type"] == "documentation"
    assert review[1]["type"] == "book"
    assert all(item["category"] == "new" for item in review)


def test_save_requires_confirmation_then_applies_references(client, settings):
    import_id = create_reference_import(settings)
    lecture_import.process_import(settings, import_id)
    complete_source_jobs(settings)
    try:
        save_import(settings, import_id, "Process Analysis")
    except ValueError as exc:
        assert "Подтвердите" in str(exc)
    else:
        raise AssertionError("Unconfirmed references were saved")
    login_admin(client)
    confirm_all(client, import_id)
    lecture_id, slug, _ = save_import(settings, import_id, "Process Analysis")
    conn = connect(settings)
    try:
        term = conn.execute("SELECT * FROM terms").fetchone()
        assert term["term"] == "BPMN"
        assert term["status"] == "verified"
        assert conn.execute("SELECT COUNT(*) FROM term_refs WHERE term_id = ?", (term["id"],)).fetchone()[0] == 1
        sources = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
        assert len(sources) == 2
        assert sources[0]["type"] == "documentation"
        assert conn.execute("SELECT COUNT(*) FROM source_refs").fetchone()[0] == 2
    finally:
        conn.close()


def test_conflict_detection_and_teacher_choice(client, settings):
    lecture_id, slug, _ = saved_references(settings, client)
    second = create_lecture_import(
        settings,
        titles=("Second lecture",),
        notes=("Overview\nBPMN has another meaning here.\nСокращения: BPMN — Business Process Model and Notation, другой вариант",),
    )
    lecture_import.process_import(settings, second)
    proposals = glossary.collect_term_proposals(settings, second)
    assert len(proposals) == 1
    assert proposals[0]["category"] == "conflict"
    response = client.get(f"/admin/lectures/import/{second}")
    response = client.post(
        f"/admin/lectures/import/{second}/confirm",
        data={
            "csrf": csrf_from(response), "terms": ["0"], "variant_0": "0",
            "expansion_0": "Business Process Modeling Notation",
            "english_0": "BPMN",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    save_import(settings, second, "Second lecture")
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == 1
        term = conn.execute("SELECT * FROM terms").fetchone()
        assert term["expansion"] == "Business Process Modeling Notation"
        assert conn.execute("SELECT COUNT(*) FROM term_refs WHERE term_id = ?", (term["id"],)).fetchone()[0] == 2
    finally:
        conn.close()


def test_student_glossary_highlight_citation_and_visibility(client, settings):
    lecture_id, slug, _ = saved_references(settings, client)
    conn = connect(settings)
    try:
        conn.execute("UPDATE lectures SET status = 'open' WHERE id = ?", (lecture_id,))
    finally:
        conn.close()
    login_student(client, "reference_student", 1)
    response = client.get("/glossary")
    assert "BPMN" in response.text
    assert "Business Process Model" in response.text
    response = client.get(f"/lectures/{slug}")
    assert '<abbr class="term"' in response.text
    assert f'/literature/{slug}/source/1' in response.text
    response = client.get(f"/literature/{slug}/source/1", follow_redirects=False)
    assert response.status_code == 303
    assert "#source-1" in response.headers["location"]
    response = client.get("/literature?priority=main")
    assert "Design Patterns" not in response.text
    response = client.get("/literature?priority=extra")
    assert "Design Patterns" in response.text
    conn = connect(settings)
    try:
        conn.execute("UPDATE lectures SET status = 'hidden' WHERE id = ?", (lecture_id,))
    finally:
        conn.close()
    response = client.get("/glossary")
    assert "Термины не найдены" in response.text


def test_admin_edit_hides_term(client, settings):
    saved_references(settings, client)
    response = client.get("/admin/glossary")
    assert "BPMN" in response.text
    assert "Слайдов с термином: 1" in response.text
    assert "Ссылки: 1" not in response.text
    response = client.post(
        "/admin/glossary/1",
        data={
            "csrf": csrf_from(response), "term": "BPMN", "expansion": "Business Process Model",
            "english": "BPMN", "status": "hidden",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        assert conn.execute("SELECT status FROM terms").fetchone()[0] == "hidden"
    finally:
        conn.close()


def test_admin_deletes_term_and_its_reference_links(client, settings):
    saved_references(settings, client)
    response = client.get("/admin/glossary")
    assert 'formaction="/admin/glossary/1/delete"' in response.text
    response = client.post(
        "/admin/glossary/1/delete",
        data={"csrf": csrf_from(response)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/glossary"
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM term_refs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM term_conflicts").fetchone()[0] == 0
    finally:
        conn.close()
    assert "BPMN" not in client.get("/admin/glossary").text


def test_admin_deletes_source_and_its_reference_links(client, settings):
    saved_references(settings, client)
    response = client.get("/admin/literature")
    assert 'formaction="/admin/literature/1/delete"' in response.text
    response = client.post(
        "/admin/literature/1/delete",
        data={"csrf": csrf_from(response)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/literature"
    conn = connect(settings)
    try:
        remaining = conn.execute("SELECT id, title FROM sources").fetchall()
        assert [row["title"] for row in remaining] == ["Design Patterns"]
        assert conn.execute(
            "SELECT COUNT(*) FROM source_refs WHERE source_id = 1"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM source_refs").fetchone()[0] == 1
    finally:
        conn.close()
    page = client.get("/admin/literature").text
    assert "Analysis Patterns" not in page
    assert "Design Patterns" in page


def test_update_replaces_old_reference_links(client, settings):
    lecture_id, slug, _ = saved_references(settings, client)
    second = create_lecture_import(
        settings,
        titles=("Process Analysis",),
        notes=("Overview\nKPI measures results.\nСокращения: KPI — Key Performance Indicator",),
    )
    lecture_import.process_import(settings, second)
    response = client.get(f"/admin/lectures/import/{second}")
    response = client.post(
        f"/admin/lectures/import/{second}/confirm",
        data={
            "csrf": csrf_from(response), "terms": ["0"], "variant_0": "0",
            "expansion_0": "Key Performance Indicator", "english_0": "KPI",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    save_import(settings, second, "Process Analysis")
    conn = connect(settings)
    try:
        old = conn.execute("SELECT * FROM terms WHERE term = 'BPMN'").fetchone()
        new = conn.execute("SELECT * FROM terms WHERE term = 'KPI'").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM term_refs WHERE term_id = ?", (old["id"],)).fetchone()[0] == 0
        assert old["unused"] == 1
        assert conn.execute("SELECT COUNT(*) FROM term_refs WHERE term_id = ?", (new["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM source_refs WHERE lecture_id = ?", (lecture_id,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_export_formats_return_real_files(client, settings):
    lecture_id, slug, _ = saved_references(settings, client)
    conn = connect(settings)
    try:
        conn.execute("UPDATE lectures SET status = 'open' WHERE id = ?", (lecture_id,))
    finally:
        conn.close()
    login_student(client, "export_student", 1)
    for fmt in ("xlsx", "md", "docx", "pdf"):
        response = client.get(f"/glossary/export/{fmt}")
        assert response.status_code == 200, fmt
        assert len(response.content) > 100, fmt
        if fmt in {"xlsx", "docx"}:
            assert response.content[:2] == b"PK"
        if fmt == "docx":
            import io
            from docx import Document
            from docx.shared import Mm
            document = Document(io.BytesIO(response.content))
            footer_images = document.sections[0].footer._element.xpath(".//wp:inline")
            assert len(footer_images) == 1
            extent = footer_images[0].xpath("./wp:extent")[0]
            width = int(extent.get("cx"))
            height = int(extent.get("cy"))
            assert abs(height - int(Mm(12))) <= 1
            assert abs(width / height - 3) < 0.01
        if fmt == "pdf":
            assert response.content.startswith(b"%PDF")
            import fitz
            with fitz.open(stream=response.content, filetype="pdf") as document:
                assert "BPMN" in "".join(page.get_text() for page in document)
                rectangles = [
                    rect
                    for page in document
                    for image in page.get_images(full=True)
                    for rect in page.get_image_rects(image[0])
                ]
                assert any(
                    abs(rect.width / rect.height - 3) < 0.01 and abs(rect.height - 34) < 0.1
                    for rect in rectangles
                )
        if fmt == "md":
            assert "BPMN" in response.text
    for fmt in ("md", "docx", "pdf"):
        response = client.get(f"/literature/export/{fmt}")
        assert response.status_code == 200, fmt
        assert len(response.content) > 100
    response = client.get("/glossary/powerpoint.txt")
    assert response.text.startswith("Сокращения: BPMN")
