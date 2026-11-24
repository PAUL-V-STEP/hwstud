from __future__ import annotations

import asyncio
import json
import zipfile

from app.db import connect
from app.services import lecture_import
from app.services.proxyapi import Outcome, ProxyResult
from app.worker import Worker, claim_next_job

from .lecture_helpers import create_lecture_import, import_row, report


def test_parse_pptx_pdf_and_render(settings):
    import_id = create_lecture_import(settings)
    lecture_import.process_import(settings, import_id)
    row = import_row(settings, import_id)
    assert row["status"] == "ready"
    data = report(settings, import_id)
    assert data["title"] == "Process Analysis"
    assert len(data["slides"]) == 2
    assert "Overview" in data["slides"][0]["notes_html"]
    assert not data["errors"]
    slide = settings.data_dir / "import" / str(import_id) / "slides" / "1.webp"
    thumb = slide.parent / "thumb" / "1.webp"
    assert slide.is_file() and slide.stat().st_size > 1000
    assert thumb.is_file() and thumb.stat().st_size > 500


def test_mismatched_slide_count_blocks_import(settings):
    import_id = create_lecture_import(settings, titles=("Only one",), notes=("Overview\nText",))
    pdf_path = settings.data_dir / "import" / str(import_id) / "source.pdf"
    import fitz
    pdf = fitz.open()
    pdf.new_page()
    pdf.new_page()
    pdf.save(pdf_path)
    pdf.close()
    lecture_import.process_import(settings, import_id)
    assert import_row(settings, import_id)["status"] == "failed"
    assert "Число слайдов не совпадает" in report(settings, import_id)["errors"][0]


def test_missing_notes_is_blocking_only_when_all_empty(settings):
    import_id = create_lecture_import(
        settings,
        titles=("One", "Two"),
        notes=("", "Title\nOnly one slide has notes"),
    )
    lecture_import.process_import(settings, import_id)
    assert import_row(settings, import_id)["status"] == "ready"
    assert "Слайды без заметок: 1" in report(settings, import_id)["warnings"]


def test_abbreviation_batching_creates_all_packets_without_network(settings):
    terms = [f"AA{index}" for index in range(65)]
    notes = "Overview\n" + " ".join(f"{term} is present." for term in terms)
    import_id = create_lecture_import(
        settings, titles=("Abbreviations",), notes=(notes,)
    )
    lecture_import.process_import(settings, import_id)
    assert import_row(settings, import_id)["status"] == "waiting_gpt"
    assert report(settings, import_id)["models_confirmed"] is False
    conn = connect(settings)
    try:
        jobs = conn.execute(
            "SELECT * FROM jobs WHERE kind = 'abbr_guess' ORDER BY id"
        ).fetchall()
        assert len(jobs) == 2
        sizes = [len(json.loads(job["payload_json"])["items"]) for job in jobs]
        assert sizes == [60, 5]
        assert claim_next_job(settings) is None
    finally:
        conn.close()


def test_abbreviation_occurrences_include_repeated_slides(settings):
    import_id = create_lecture_import(
        settings,
        titles=("First", "Second", "Third"),
        notes=(
            "Overview\nBPMN is introduced.",
            "Roles\nOrdinary text without abbreviations.",
            "Review\nBPMN returns. BPMN is repeated.",
        ),
    )
    lecture_import.process_import(settings, import_id)
    data = report(settings, import_id)
    occurrence = next(
        item for item in data["term_occurrences"] if item["term"] == "BPMN"
    )
    assert occurrence["slide_numbers"] == [1, 3]
    conn = connect(settings)
    try:
        job = conn.execute("SELECT payload_json FROM jobs WHERE kind = 'abbr_guess'").fetchone()
        candidate = json.loads(job["payload_json"])["items"][0]
        assert candidate["slide_numbers"] == [1, 3]
    finally:
        conn.close()


def test_sections_and_repeated_header_lines(settings):
    notes = (
        "Overview\nConditional data\nFirst part",
        "Roles\nConditional data\nSecond part",
    )
    import_id = create_lecture_import(settings, notes=notes)
    lecture_import.process_import(settings, import_id)
    data = report(settings, import_id)
    assert data["header_lines"] == ["Conditional data"]
    assert "Conditional data" not in data["slides"][0]["notes_html"]
    assert "First part" in data["slides"][0]["notes_html"]


def test_extract_powerpoint_sections(settings, tmp_path):
    path = tmp_path / "sections.pptx"
    xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
      xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main">
      <p:sldIdLst><p:sldId id="256"/><p:sldId id="257"/><p:sldId id="258"/></p:sldIdLst>
      <p:extLst><p:ext><p14:sectionLst>
        <p14:section name="Start"><p14:sldIdLst><p14:sldId id="256"/></p14:sldIdLst></p14:section>
        <p14:section name="Details"><p14:sldIdLst><p14:sldId id="258"/></p14:sldIdLst></p14:section>
      </p14:sectionLst></p:ext></p:extLst>
    </p:presentation>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/presentation.xml", xml)
    sections = lecture_import.extract_sections(path, ["256", "257", "258"])
    assert sections == [
        {"idx": 0, "title": "Start", "first_slide": 1},
        {"idx": 1, "title": "Details", "first_slide": 3},
    ]


def test_zip_compression_ratio_is_rejected(tmp_path):
    path = tmp_path / "bomb.pptx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ppt/presentation.xml", "<xml/>")
        archive.writestr("large.txt", "0" * 100_000)
    try:
        lecture_import.validate_pptx(path)
    except ValueError as exc:
        assert "100:1" in str(exc)
    else:
        raise AssertionError("ZIP with excessive compression was accepted")


class FakeProxyClient:
    last_kwargs = None

    async def chat_completion(self, **_kwargs):
        type(self).last_kwargs = _kwargs
        return ProxyResult(
            Outcome.SUCCESS,
            status_code=200,
            content={
                "items": [
                    {"term": "MoSCoW", "expansion": "Must, Should, Could, Won't", "english": ""}
                ]
            },
            model="test-model",
            tokens_in=10,
            tokens_out=5,
        )


def test_gpt_completion_uses_stub_and_releases_preview(settings):
    import_id = create_lecture_import(
        settings,
        titles=("Priorities",),
        notes=("Overview\nPrioritisation example.\nСокращения: MoSCoW: Must; Should; Could; Won't",),
    )
    lecture_import.process_import(settings, import_id)
    assert import_row(settings, import_id)["status"] == "waiting_gpt"
    conn = connect(settings)
    try:
        conn.execute("UPDATE settings SET value = '1' WHERE key = 'models_confirmed'")
    finally:
        conn.close()
    job = claim_next_job(settings)
    assert job["kind"] == "parse_abbr"
    worker = Worker(settings, proxy_client=FakeProxyClient())
    asyncio.run(worker._run_job(dict(job)))
    assert import_row(settings, import_id)["status"] == "ready"
    conn = connect(settings)
    try:
        saved = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        assert saved["status"] == "done"
        assert saved["tokens_in"] == 10
        assert saved["cost_est"] == 0.00105
        assert FakeProxyClient.last_kwargs["max_completion_tokens"] == 2000
        assert FakeProxyClient.last_kwargs["reasoning_effort"] == "low"
        assert FakeProxyClient.last_kwargs["response_format"]["type"] == "json_schema"
        result = json.loads(saved["payload_json"])["result"]
        assert result["items"][0]["term"] == "MoSCoW"
    finally:
        conn.close()


def test_ready_preview_expires(settings):
    import_id = create_lecture_import(settings)
    lecture_import.process_import(settings, import_id)
    conn = connect(settings)
    try:
        conn.execute(
            "UPDATE imports SET ready_at = '2020-01-01T00:00:00Z' WHERE id = ?",
            (import_id,),
        )
    finally:
        conn.close()
    lecture_import.expire_ready_imports(settings)
    assert import_row(settings, import_id)["status"] == "expired"
