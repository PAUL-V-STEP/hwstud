from __future__ import annotations

import base64
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import fitz
from PIL import Image
from docx import Document
from docx.document import Document as DocumentType
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from ..config import Settings
from ..db import transaction, utc_now_iso

MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_TEXT_CHARS = 30_000
MAX_PDF_PAGES = 50
MAX_PDF_IMAGES = 5
MAX_DOCX_IMAGES = 5
MAX_DOCX_UNPACKED = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

ALLOWED_TRANSITIONS = {
    "not_submitted": {"queued"},
    "queued": {"checking", "needs_review"},
    "checking": {"awaiting_approval", "needs_review", "queued"},
    "awaiting_approval": {"accepted", "revise", "exhausted", "queued"},
    "needs_review": {"accepted", "revise", "exhausted", "queued"},
    "revise": {"queued", "exhausted"},
    "accepted": set(),
    "exhausted": set(),
}
STUDENT_STATUS = {
    "not_submitted": "Не сдано",
    "queued": "На проверке",
    "checking": "На проверке",
    "awaiting_approval": "На проверке",
    "needs_review": "На проверке",
    "revise": "На доработку",
    "accepted": "Принята",
    "exhausted": "Попытки исчерпаны",
}


class WorkValidationError(ValueError):
    pass


@dataclass(slots=True)
class PreparedWork:
    extension: str
    mime: str
    size: int
    pages: int | None
    chars: int
    parts: list[dict[str, Any]] = field(default_factory=list)
    images: int = 0

    def write_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")

    def to_dict(self) -> dict[str, Any]:
        return {
            "extension": self.extension,
            "mime": self.mime,
            "size": self.size,
            "pages": self.pages,
            "chars": self.chars,
            "parts": self.parts,
            "images": self.images,
        }


def transition(conn, submission_id: int, new_status: str) -> None:
    row = conn.execute(
        "SELECT * FROM submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    if not row:
        raise ValueError("Работа не найдена")
    if new_status not in ALLOWED_TRANSITIONS[row["status"]]:
        raise ValueError(f"Недопустимый переход {row['status']} → {new_status}")
    decided = new_status in {"accepted", "revise", "exhausted"}
    conn.execute(
        "UPDATE submissions SET status = ?, decided_at = CASE WHEN ? THEN ? ELSE decided_at END "
        "WHERE id = ?",
        (new_status, int(decided), utc_now_iso(), submission_id),
    )


def can_upload(latest) -> bool:
    return not latest or (latest["status"] == "revise" and latest["attempt"] < 4)


def teacher_decision(
    conn, submission_id: int, verdict: str, score: int, comment: str
) -> str:
    row = conn.execute(
        "SELECT * FROM submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    if not row:
        raise ValueError("Работа не найдена")
    if row["status"] not in {"awaiting_approval", "needs_review"}:
        raise ValueError("Работа ещё не ждёт решения преподавателя")
    if verdict not in {"accept", "revise"}:
        raise ValueError("Неверный вердикт")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 10:
        raise ValueError("Баллы должны быть целым числом от 0 до 10")
    new_status = "accepted" if verdict == "accept" else (
        "exhausted" if row["attempt"] == 4 else "revise"
    )
    with transaction(conn, immediate=True):
        conn.execute(
            "UPDATE submissions SET verdict = ?, score = ?, comment = ? WHERE id = ?",
            (verdict, score, comment.strip(), submission_id),
        )
        transition(conn, submission_id, new_status)
    return new_status


def detect_file(path: Path) -> str:
    with path.open("rb") as source:
        signature = source.read(16)
    if signature.startswith(b"%PDF-"):
        return "pdf"
    if signature.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if signature.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if "word/document.xml" in archive.namelist():
                return "docx"
        raise WorkValidationError("Архив ZIP не является DOCX")
    try:
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise WorkValidationError("Формат файла не поддерживается")
        raw.decode("utf-8")
        return "txt"
    except UnicodeError:
        try:
            raw.decode("cp1251")
            return "txt"
        except UnicodeError as exc:
            raise WorkValidationError("Формат файла не поддерживается") from exc


def prepare_file(path: Path) -> PreparedWork:
    deadline = time.monotonic() + 60
    size = path.stat().st_size
    if size > MAX_FILE_SIZE:
        raise WorkValidationError("Файл больше 10 МБ")
    extension = detect_file(path)
    if extension == "pdf":
        return _prepare_pdf(path, size, deadline)
    if extension in {"jpg", "png"}:
        return _prepare_image(path, size, extension, deadline)
    if extension == "docx":
        return _prepare_docx(path, size, deadline)
    return _prepare_txt(path, size)


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise WorkValidationError("Файл не удалось прочитать за отведённое время")


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _image_part(data: bytes, media_type: str) -> dict[str, Any]:
    return {
        "type": "image",
        "media_type": media_type,
        "data": base64.b64encode(data).decode("ascii"),
    }


def _prepare_txt(path: Path, size: int) -> PreparedWork:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        text = raw.decode("cp1251")
    if len(text) > MAX_TEXT_CHARS:
        raise WorkValidationError("Текст больше 30 000 знаков")
    return PreparedWork("txt", "text/plain", size, None, len(text), [_text_part(text)])


def _prepare_pdf(path: Path, size: int, deadline: float) -> PreparedWork:
    document = fitz.open(path)
    parts = []
    chars = 0
    images = 0
    try:
        for index, page in enumerate(document, start=1):
            _check_deadline(deadline)
            if index > MAX_PDF_PAGES:
                raise WorkValidationError("PDF содержит больше 50 страниц")
            text = page.get_text().strip()
            if len(text) >= 200:
                chars += len(text)
                if chars > MAX_TEXT_CHARS:
                    raise WorkValidationError("Текст PDF больше 30 000 знаков")
                parts.append(_text_part(f"[Страница {index}]\n{text}"))
            else:
                images += 1
                if images > MAX_PDF_IMAGES:
                    raise WorkValidationError("В PDF больше 5 страниц-сканов")
                data, media_type = _render_pdf_page(page)
                _check_deadline(deadline)
                parts.append(_image_part(data, media_type))
        return PreparedWork("pdf", "application/pdf", size, len(document), chars, parts, images)
    finally:
        document.close()


def _render_pdf_page(page) -> tuple[bytes, str]:
    scale = min(1.0, 1600 / page.rect.width)
    if page.rect.width * scale * page.rect.height * scale > MAX_IMAGE_PIXELS:
        raise WorkValidationError("Страница-скан превышает лимит 40 млн пикселей")
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return pixmap.tobytes("png"), "image/png"


def _prepare_image(path: Path, size: int, extension: str, deadline: float) -> PreparedWork:
    _check_deadline(deadline)
    data, media_type = _read_image(path, expected=extension)
    _check_deadline(deadline)
    return PreparedWork(
        extension,
        "image/jpeg" if extension == "jpg" else "image/png",
        size,
        1,
        0,
        [_image_part(data, media_type)],
        1,
    )


def _read_image(path_or_bytes, expected: str | None = None) -> tuple[bytes, str]:
    try:
        source = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
        with Image.open(source) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise WorkValidationError("Изображение содержит больше 40 млн пикселей")
            if getattr(image, "n_frames", 1) != 1:
                raise WorkValidationError("Нужно одно изображение, не анимация")
            image.load()
            if expected == "jpg" and image.format != "JPEG":
                raise WorkValidationError("Содержимое файла не соответствует JPG")
            if expected == "png" and image.format != "PNG":
                raise WorkValidationError("Содержимое файла не соответствует PNG")
            if image.format not in {"JPEG", "PNG"}:
                raise WorkValidationError("Изображения в DOCX должны быть JPEG или PNG")
            if max(image.size) > 1600:
                scale = 1600 / max(image.size)
                image = image.resize(
                    (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            output = io.BytesIO()
            if image.format == "JPEG":
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                image.save(output, "JPEG", quality=90)
                return output.getvalue(), "image/jpeg"
            if image.mode not in {"RGB", "RGBA", "L"}:
                image = image.convert("RGBA")
            image.save(output, "PNG")
            return output.getvalue(), "image/png"
    except WorkValidationError:
        raise
    except Exception as exc:
        raise WorkValidationError("Не удалось прочитать изображение") from exc


def _validate_docx_archive(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 1000:
            raise WorkValidationError("DOCX содержит больше 1000 элементов")
        if sum(info.file_size for info in infos) > MAX_DOCX_UNPACKED:
            raise WorkValidationError("DOCX после распаковки больше 50 МБ")
        for info in infos:
            if info.compress_size and info.file_size / info.compress_size > 100:
                raise WorkValidationError("DOCX содержит элемент со сжатием больше 100:1")


def _prepare_docx(path: Path, size: int, deadline: float) -> PreparedWork:
    _validate_docx_archive(path)
    document = Document(str(path))
    parts: list[dict[str, Any]] = []
    text_parts: list[str] = []
    images = 0
    for block in _iter_blocks(document):
        _check_deadline(deadline)
        if isinstance(block, Paragraph):
            for piece in _paragraph_pieces(block):
                _check_deadline(deadline)
                if isinstance(piece, str):
                    if piece:
                        text_parts.append(piece)
                        parts.append(_text_part(piece))
                else:
                    images += 1
                    if images > MAX_DOCX_IMAGES:
                        raise WorkValidationError("В DOCX больше 5 изображений")
                    data, media_type = _read_image(piece)
                    _check_deadline(deadline)
                    parts.append(_image_part(data, media_type))
        elif isinstance(block, Table):
            for row in block.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        for piece in _paragraph_pieces(paragraph):
                            _check_deadline(deadline)
                            if isinstance(piece, str):
                                if piece:
                                    text_parts.append(piece)
                                    parts.append(_text_part(piece))
                            else:
                                images += 1
                                if images > MAX_DOCX_IMAGES:
                                    raise WorkValidationError("В DOCX больше 5 изображений")
                                data, media_type = _read_image(piece)
                                parts.append(_image_part(data, media_type))
        parts.append(_text_part("\n"))
    chars = sum(
        len(part["text"])
        for part in parts
        if part["type"] == "text" and part["text"] != "\n"
    )
    if chars > MAX_TEXT_CHARS:
        raise WorkValidationError("Текст DOCX больше 30 000 знаков")
    return PreparedWork(
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size,
        None,
        chars,
        parts,
        images,
    )


def _iter_blocks(parent) -> Iterator[Paragraph | Table]:
    if isinstance(parent, DocumentType):
        element = parent.element.body
    elif isinstance(parent, _Cell):
        element = parent._tc
    else:
        raise TypeError("Unsupported parent")
    for child in element.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def _paragraph_pieces(paragraph: Paragraph) -> Iterator[str | bytes]:
    for run in paragraph._p.xpath(".//w:r"):
        for element in run.iter():
            if element.tag.endswith("}t") and element.text:
                yield element.text
            elif element.tag.endswith("}tab"):
                yield "\t"
            elif element.tag.endswith("}br"):
                yield "\n"
            elif element.tag.endswith("}blip"):
                relationship = element.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
                )
                if relationship:
                    yield paragraph.part.related_parts[relationship].blob


def prepared_messages(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    content = []
    for part in data["parts"]:
        if part["type"] == "text":
            if part["text"]:
                content.append({"type": "text", "text": part["text"]})
        else:
            url = f"data:{part['media_type']};base64,{part['data']}"
            content.append({"type": "image_url", "image_url": {"url": url}})
    return content
