from __future__ import annotations

import io
from pathlib import Path
from typing import Iterable

import fitz
from docx import Document
from docx.shared import Mm, Pt
from openpyxl import Workbook
from PIL import Image

from ..config import Settings
from ..db import parse_utc


def export_records(
    settings: Settings,
    kind: str,
    rows: Iterable,
    fmt: str,
) -> tuple[bytes, str, str]:
    rows = list(rows)
    if kind == "glossary":
        title = "Глоссарий курса"
        lines = [
            f"{row['term']} — {row['expansion']}"
            + (f" ({row['english']})" if row["english"] else "")
            + f" — {row['lecture_title']}"
            for row in rows
        ]
    elif kind == "literature":
        title = "Литература курса"
        lines = [
            ". ".join(filter(None, (row["author"], row["title"])))
            + (f" — {row['url']}" if row["url"] else "")
            + f"\n{row['annotation'] or ''}\n{row['lecture_title']}, слайды {row['slide_numbers'] or '—'}"
            for row in rows
        ]
    else:
        raise ValueError("Неизвестный вид выгрузки")

    if fmt == "md":
        text = f"# {title}\n\n" + "\n\n".join(f"- {line}" for line in lines) + "\n"
        return text.encode("utf-8"), "text/markdown; charset=utf-8", f"{kind}.md"
    if fmt == "xlsx" and kind == "glossary":
        return _xlsx(rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "glossary.xlsx"
    if fmt == "docx":
        return _docx(settings, title, lines), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", f"{kind}.docx"
    if fmt == "pdf":
        return _pdf(settings, title, lines), "application/pdf", f"{kind}.pdf"
    raise ValueError("Неподдерживаемый формат выгрузки")


def export_assignment_pdf(settings: Settings, assignment) -> bytes:
    due_at = parse_utc(assignment["due_at"])
    due_text = (
        due_at.astimezone(settings.timezone).strftime("%d.%m.%Y %H:%M МСК")
        if due_at
        else "не указан"
    )
    title = f"Задание {assignment['position']}. {assignment['title']}"
    lines = [
        f"Срок сдачи: {due_text}",
        assignment["condition"] or "Условие будет добавлено преподавателем.",
    ]
    return _pdf(settings, title, lines)


def _xlsx(rows: list) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Глоссарий"
    sheet.append(("Термин", "Расшифровка", "English", "Лекция"))
    for row in rows:
        sheet.append((row["term"], row["expansion"], row["english"], row["lecture_title"]))
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in zip("ABCD", (24, 60, 40, 40)):
        sheet.column_dimensions[column].width = width
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _docx(settings: Settings, title: str, lines: list[str]) -> bytes:
    document = Document()
    normal = document.styles["Normal"]
    normal.font.name = "IBM Plex Sans"
    normal.font.size = Pt(11)
    document.add_heading(title, level=1)
    for line in lines:
        document.add_paragraph(line, style="List Bullet")
    footer = document.sections[0].footer.paragraphs[0]
    footer.add_run("AI&HI · hwstud.ru\n")
    footer.add_run().add_picture(
        io.BytesIO(_logo_png(settings)),
        height=Mm(12),
    )
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pdf(settings: Settings, title: str, lines: list[str]) -> bytes:
    font_path = settings.base_dir / "app" / "fonts" / "IBMPlexSans-Regular.ttf"
    font = fitz.Font(fontfile=str(font_path))
    document = fitz.open()
    page = None
    y = 0

    def new_page():
        nonlocal page, y
        page = document.new_page(width=595, height=842)
        y = 50

    def write(text: str, size: int = 11, width: float = 495) -> None:
        nonlocal y
        for line in _wrap(font, text, width, size):
            if y > 790:
                new_page()
            page.insert_text(
                (50, y),
                line,
                fontsize=size,
                fontname="IBMPlexSans",
                fontfile=str(font_path),
                color=(0.12, 0.16, 0.2),
            )
            y += size * 1.45
        y += 6

    new_page()
    write(title, 18)
    for line in lines:
        write(line)
    logo = _logo_png(settings)
    for page in document:
        page.insert_image(
            fitz.Rect(50, 800, 152, 834),
            stream=logo,
        )
    return document.tobytes(deflate=True)


def _logo_png(settings: Settings) -> bytes:
    with Image.open(settings.base_dir / "static" / "brand" / "logo-on-light.webp") as image:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def _wrap(font, text: str, width: float, size: int) -> list[str]:
    result: list[str] = []
    for source_line in text.splitlines() or [""]:
        words = source_line.split()
        current = ""
        for word in words:
            while font.text_length(word, fontsize=size) > width:
                piece = ""
                for char in word:
                    if font.text_length(piece + char, fontsize=size) > width:
                        if current:
                            result.append(current)
                            current = ""
                        if piece:
                            result.append(piece)
                        piece = char
                    else:
                        piece += char
                word = piece
            candidate = f"{current} {word}".strip()
            if current and font.text_length(candidate, fontsize=size) > width:
                result.append(current)
                current = word
            else:
                current = candidate
        result.append(current)
    return result
