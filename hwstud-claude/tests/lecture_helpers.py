from __future__ import annotations

import json
from pathlib import Path

import fitz
from pptx import Presentation

from app.db import connect, migrate, utc_now_iso


def create_lecture_import(
    settings,
    *,
    titles=("Process Analysis", "Roles and Tasks"),
    notes=(
        "Overview\nThe process has a clear start and an end.",
        "Roles\nEvery task has a responsible person.",
    ),
) -> int:
    migrate(settings)
    conn = connect(settings)
    try:
        cursor = conn.execute(
            "INSERT INTO imports(created_at, status, report_json) VALUES (?, 'processing', '{}')",
            (utc_now_iso(),),
        )
        import_id = int(cursor.lastrowid)
    finally:
        conn.close()
    folder = settings.data_dir / "import" / str(import_id)
    folder.mkdir(parents=True)

    presentation = Presentation()
    for title, note in zip(titles, notes, strict=True):
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = title
        slide.notes_slide.notes_text_frame.text = note
    presentation.save(folder / "source.pptx")

    pdf = fitz.open()
    for title in titles:
        page = pdf.new_page(width=1600, height=900)
        page.insert_text((90, 110), title, fontsize=40)
        page.insert_text((90, 190), "Course slide for local automated tests", fontsize=24)
    pdf.save(folder / "source.pdf")
    pdf.close()
    return import_id


def import_row(settings, import_id: int):
    conn = connect(settings)
    try:
        return conn.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
    finally:
        conn.close()


def report(settings, import_id: int):
    return json.loads(import_row(settings, import_id)["report_json"])
