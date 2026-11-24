from __future__ import annotations

import html
import io
import json
import re
import shutil
import unicodedata
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import fitz
from lxml import etree
from PIL import Image
from pptx import Presentation

from ..config import Settings
from ..db import connect, parse_utc, transaction, utc_now, utc_now_iso
from . import literature

ACTIVE_IMPORTS = ("processing", "waiting_gpt", "paused_no_funds", "ready", "saving")
MAX_PPTX_UNPACKED = 300 * 1024 * 1024
MAX_PPTX_ELEMENTS = 5000
MAX_PDF_PAGES = 300

ABBREVIATION_RE = re.compile(r"(?<![\w])[A-ZА-ЯЁ0-9&-]{2,10}(?![\w])")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "iu", "я": "ia",
}


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("­", "").replace(" ", " ")
    value = value.lower().replace("ё", "е")
    value = "".join(char if char.isalnum() else " " for char in value)
    return " ".join(value.split())


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower().replace("ё", "е")
    result = "".join(TRANSLIT.get(char, char) for char in normalized)
    result = re.sub(r"[^a-z0-9]+", "-", result).strip("-")
    return result[:60].rstrip("-") or "lecture"


def validate_pptx(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_PPTX_ELEMENTS:
            raise ValueError("PPTX содержит больше 5000 элементов")
        total = sum(info.file_size for info in infos)
        if total > MAX_PPTX_UNPACKED:
            raise ValueError("PPTX после распаковки больше 300 МБ")
        for info in infos:
            if info.compress_size and info.file_size / info.compress_size > 100:
                raise ValueError("PPTX содержит элемент со сжатием больше 100:1")
        if "ppt/presentation.xml" not in archive.namelist():
            raise ValueError("Это не презентация PPTX")


def _slide_title(slide) -> str:
    if slide.shapes.title is not None:
        title = slide.shapes.title.text.strip()
        if title:
            return title
    for shape in slide.shapes:
        if hasattr(shape, "text") and shape.text.strip():
            return shape.text.strip().splitlines()[0]
    return ""


def _slide_notes(slide) -> str:
    if not slide.has_notes_slide:
        return ""
    frame = slide.notes_slide.notes_text_frame
    if frame is None:
        return ""
    return frame.text.strip()


def _notes_parts(notes: str) -> tuple[list[str], list[str]]:
    visible: list[str] = []
    abbreviations: list[str] = []
    for line in notes.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("сокращения:"):
            value = stripped.split(":", 1)[1].strip()
            if value:
                abbreviations.append(value)
        elif stripped:
            visible.append(stripped)
    return visible, abbreviations


def notes_html(lines: list[str]) -> str:
    if not lines:
        return ""
    first = html.escape(lines[0])
    parts = [f"<p><strong>{first}</strong></p>"]
    paragraph: list[str] = []
    for line in lines[1:]:
        paragraph.append(html.escape(line))
    if paragraph:
        parts.append("<p>" + "<br>".join(paragraph) + "</p>")
    return "".join(parts)


def parse_abbreviation_block(block: str) -> tuple[list[dict[str, str]], bool]:
    chunks = [chunk.strip() for chunk in block.split(";") if chunk.strip()]
    items: list[dict[str, str]] = []
    complex_block = False
    for chunk in chunks:
        match = re.match(r"^(.+?)(?:\s+[—–-]\s+|:\s*)(.+)$", chunk)
        if not match:
            complex_block = True
            continue
        term, expansion = match.group(1).strip(), match.group(2).strip()
        if not term or not expansion:
            complex_block = True
            continue
        items.append({"term": term, "expansion": expansion, "english": ""})
    return items, complex_block or not items


def _abbreviation_occurrences(texts: list[str]) -> dict[str, dict[str, Any]]:
    occurrences: dict[str, dict[str, Any]] = {}
    for slide_n, text in enumerate(texts, start=1):
        sentences = SENTENCE_SPLIT_RE.split(text) or [text]
        for sentence in sentences:
            for match in ABBREVIATION_RE.finditer(sentence):
                term = match.group(0).strip("-")
                if not any(char.isalpha() for char in term):
                    continue
                letters = "".join(char for char in term if char.isalpha())
                if not letters or letters.upper() != letters:
                    continue
                normalized = normalize_title(term)
                if normalized in occurrences:
                    slides = occurrences[normalized]["slide_numbers"]
                    if slide_n not in slides:
                        slides.append(slide_n)
                    continue
                occurrences[normalized] = {
                    "term": term,
                    "context": sentence.strip(),
                    "slide_n": slide_n,
                    "slide_numbers": [slide_n],
                }
    return occurrences


def abbreviation_candidates(
    texts: list[str], known_terms: set[str], explicit_terms: set[str]
) -> list[dict[str, Any]]:
    known = {normalize_title(term) for term in known_terms | explicit_terms}
    return [
        item
        for normalized, item in _abbreviation_occurrences(texts).items()
        if normalized not in known
    ]


def extract_sections(pptx_path: Path, slide_ids: list[str]) -> list[dict[str, Any]]:
    with zipfile.ZipFile(pptx_path) as archive:
        xml = archive.read("ppt/presentation.xml")
    root = etree.fromstring(xml)
    namespaces = {
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
        "p14": "http://schemas.microsoft.com/office/powerpoint/2010/main",
    }
    positions = {slide_id: index + 1 for index, slide_id in enumerate(slide_ids)}
    sections = []
    for index, section in enumerate(root.xpath(".//p14:section", namespaces=namespaces)):
        ids = section.xpath(".//p14:sldId/@id", namespaces=namespaces)
        first = next((positions[value] for value in ids if value in positions), 1)
        sections.append(
            {"idx": index, "title": section.get("name", f"Часть {index + 1}"), "first_slide": first}
        )
    return sections


def _render_page(page, slide_dir: Path, number: int) -> None:
    scale = 1600 / page.rect.width
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    image = Image.open(io.BytesIO(pixmap.tobytes("png")))
    image.save(slide_dir / f"{number}.webp", "WEBP", quality=80)
    thumbnail = image.copy()
    thumbnail.thumbnail((320, 320))
    thumbnail.save(slide_dir / "thumb" / f"{number}.webp", "WEBP", quality=80)
    image.close()
    thumbnail.close()


def process_import(settings: Settings, import_id: int) -> None:
    conn = connect(settings)
    try:
        row = conn.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
        if not row or row["status"] != "processing":
            return
    finally:
        conn.close()

    import_dir = settings.data_dir / "import" / str(import_id)
    pptx_path = import_dir / "source.pptx"
    pdf_path = import_dir / "source.pdf"
    slide_dir = import_dir / "slides"
    slide_dir.mkdir(parents=True, exist_ok=True)
    (slide_dir / "thumb").mkdir(exist_ok=True)
    report: dict[str, Any] = {
        "title": "",
        "slides": [],
        "sections": [],
        "header_lines": [],
        "warnings": [],
        "errors": [],
        "abbreviations": [],
        "complex_abbreviations": [],
        "gpt_candidate_count": 0,
        "term_occurrences": [],
        "sources": [],
        "citations": [],
    }
    try:
        validate_pptx(pptx_path)
        presentation = Presentation(str(pptx_path))
        pdf = fitz.open(pdf_path)
        try:
            if len(pdf) > MAX_PDF_PAGES:
                raise ValueError("PDF больше 300 страниц")
            if len(presentation.slides) != len(pdf):
                raise ValueError(
                    f"Число слайдов не совпадает: PPTX {len(presentation.slides)}, PDF {len(pdf)}"
                )
            if not presentation.slides:
                raise ValueError("В презентации нет слайдов")

            visible_texts: list[str] = []
            line_occurrences: dict[str, int] = {}
            explicit_terms: set[str] = set()
            missing_notes: list[int] = []
            for index, slide in enumerate(presentation.slides, start=1):
                title = _slide_title(slide)
                notes = _slide_notes(slide)
                visible, abbreviations = _notes_parts(notes)
                visible_texts.append("\n".join(visible))
                source_items, citations = literature.extract_slide_sources(
                    title, visible, index
                )
                report["sources"].extend(source_items)
                report["citations"].extend(citations)
                for line in set(visible):
                    line_occurrences[line] = line_occurrences.get(line, 0) + 1
                for block in abbreviations:
                    items, complex_block = parse_abbreviation_block(block)
                    report["abbreviations"].extend(
                        {**item, "slide_n": index} for item in items
                    )
                    explicit_terms.update(item["term"] for item in items)
                    if complex_block:
                        report["complex_abbreviations"].append(block)
                if not visible:
                    missing_notes.append(index)
                report["slides"].append(
                    {
                        "n": index,
                        "title": title,
                        "notes_html": "",
                        "missing_notes": not visible,
                    }
                )
            if not any(visible_texts):
                raise ValueError("В PPTX нет ни одной заметки")

            threshold = len(presentation.slides) / 2
            header_lines = sorted(
                line for line, count in line_occurrences.items() if count > threshold
            )
            report["header_lines"] = header_lines
            for slide_data, notes in zip(report["slides"], visible_texts):
                lines = [
                    line for line in notes.splitlines() if line not in header_lines
                ]
                slide_data["notes_html"] = notes_html(lines)

            report["title"] = report["slides"][0]["title"].strip()
            if not report["title"]:
                raise ValueError("Не удалось определить название по первому слайду")
            report["title_norm"] = normalize_title(report["title"])
            slide_ids = presentation.slides._sldIdLst
            report["sections"] = extract_sections(
                pptx_path, [str(item.id) for item in slide_ids]
            )

            title_warnings = []
            for number, page in enumerate(pdf, start=1):
                slide_title = report["slides"][number - 1]["title"]
                if slide_title and normalize_title(slide_title) not in normalize_title(
                    page.get_text()
                ):
                    title_warnings.append(number)
                _render_page(page, slide_dir, number)
            if missing_notes:
                report["warnings"].append(
                    "Слайды без заметок: " + ", ".join(map(str, missing_notes))
                )
            if title_warnings:
                report["warnings"].append(
                    "Заголовок не найден в PDF на слайдах: "
                    + ", ".join(map(str, title_warnings))
                )

            conn = connect(settings)
            try:
                known_terms = {
                    row["term"] for row in conn.execute("SELECT term FROM terms")
                }
            finally:
                conn.close()
            occurrences = _abbreviation_occurrences(visible_texts)
            report["term_occurrences"] = [
                {"term": item["term"], "slide_numbers": item["slide_numbers"]}
                for item in occurrences.values()
            ]
            known = {normalize_title(term) for term in known_terms | explicit_terms}
            candidates = [
                item
                for normalized, item in occurrences.items()
                if normalized not in known
            ]
            report["gpt_candidate_count"] = len(candidates)
            report["sources"] = literature.merge_sources(report["sources"])
        finally:
            pdf.close()
    except Exception as exc:
        report["errors"].append(str(exc))
        _fail_import(settings, import_id, report)
        return

    _finish_parsing(settings, import_id, report, candidates)


def _finish_parsing(
    settings: Settings,
    import_id: int,
    report: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> None:
    jobs: list[tuple[str, int, str, str]] = []
    if report["complex_abbreviations"]:
        jobs.append(
            (
                "parse_abbr",
                4,
                json.dumps(
                    {"import_id": import_id, "lines": report["complex_abbreviations"]},
                    ensure_ascii=False,
                ),
                utc_now_iso(),
            )
        )
    for offset in range(0, len(candidates), 60):
        batch = candidates[offset : offset + 60]
        jobs.append(
            (
                "abbr_guess",
                4,
                json.dumps(
                    {"import_id": import_id, "batch": offset // 60, "items": batch},
                    ensure_ascii=False,
                ),
                utc_now_iso(),
            )
        )

    for source in report.get("sources", []):
        jobs.append(
            (
                "source_meta",
                5,
                json.dumps(
                    {
                        "import_id": import_id,
                        "source_index": source["source_index"],
                        "author": source["author"],
                        "title": source["title"],
                        "url": source["url"],
                        "context": report["title"],
                    },
                    ensure_ascii=False,
                ),
                utc_now_iso(),
            )
        )

    conn = connect(settings)
    try:
        with transaction(conn, immediate=True):
            balance = conn.execute(
                "SELECT paused, gpt_stopped FROM balance_state WHERE id = 1"
            ).fetchone()
            confirmed = conn.execute(
                "SELECT value FROM settings WHERE key = 'models_confirmed'"
            ).fetchone()
            report["models_confirmed"] = bool(
                confirmed and confirmed["value"] == "1"
            )
            if jobs and not report["models_confirmed"]:
                report["warnings"].append(
                    "Разбор GPT ждёт подтверждения моделей; платные запросы не отправляются."
                )
            if jobs:
                status = "paused_no_funds" if balance["paused"] or balance["gpt_stopped"] else "waiting_gpt"
            else:
                status = "ready"
            conn.execute(
                "UPDATE imports SET status = ?, ready_at = ?, report_json = ? WHERE id = ?",
                (
                    status,
                    utc_now_iso() if status == "ready" else None,
                    json.dumps(report, ensure_ascii=False),
                    import_id,
                ),
            )
            conn.executemany(
                "INSERT INTO jobs(kind, priority, payload_json, status, run_after) "
                "VALUES (?, ?, ?, 'queued', ?)",
                jobs,
            )
    finally:
        conn.close()


def _fail_import(settings: Settings, import_id: int, report: dict[str, Any]) -> None:
    conn = connect(settings)
    try:
        with transaction(conn):
            conn.execute(
                "UPDATE imports SET status = 'failed', report_json = ? WHERE id = ?",
                (json.dumps(report, ensure_ascii=False), import_id),
            )
    finally:
        conn.close()


def load_report(row) -> dict[str, Any]:
    return json.loads(row["report_json"] or "{}")


def expire_ready_imports(settings: Settings) -> None:
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT id, ready_at FROM imports WHERE status = 'ready'"
        ).fetchall()
        for row in rows:
            ready_at = parse_utc(row["ready_at"])
            if ready_at and utc_now() - ready_at > timedelta(days=7):
                conn.execute(
                    "UPDATE imports SET status = 'expired' WHERE id = ?", (row["id"],)
                )
    finally:
        conn.close()


def maybe_finish_import(settings: Settings, import_id: int) -> None:
    conn = connect(settings)
    try:
        with transaction(conn, immediate=True):
            row = conn.execute(
                "SELECT * FROM imports WHERE id = ?", (import_id,)
            ).fetchone()
            if not row or row["status"] not in {"waiting_gpt", "paused_no_funds"}:
                return
            jobs = conn.execute(
                "SELECT * FROM jobs WHERE json_extract(payload_json, '$.import_id') = ? "
                "AND kind IN ('parse_abbr', 'abbr_guess', 'source_meta')",
                (import_id,),
            ).fetchall()
            if not jobs:
                return
            if any(job["status"] == "failed" for job in jobs):
                report = load_report(row)
                report["errors"].append("Служебный разбор GPT завершился ошибкой")
                conn.execute(
                    "UPDATE imports SET status = 'failed', report_json = ? WHERE id = ?",
                    (json.dumps(report, ensure_ascii=False), import_id),
                )
                return
            if all(job["status"] == "done" for job in jobs):
                conn.execute(
                    "UPDATE imports SET status = 'ready', ready_at = ? WHERE id = ?",
                    (utc_now_iso(), import_id),
                )
            else:
                balance = conn.execute(
                    "SELECT paused, gpt_stopped FROM balance_state WHERE id = 1"
                ).fetchone()
                status = (
                    "paused_no_funds"
                    if balance["paused"] or balance["gpt_stopped"]
                    else "waiting_gpt"
                )
                conn.execute(
                    "UPDATE imports SET status = ? WHERE id = ?", (status, import_id)
                )
    finally:
        conn.close()


def recover_saving_imports(settings: Settings) -> None:
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT * FROM imports WHERE status = 'saving'"
        ).fetchall()
        for row in rows:
            version_dir = Path(row["version_dir"] or "")
            if not version_dir.is_absolute():
                version_dir = settings.base_dir / version_dir
            in_use = conn.execute(
                "SELECT 1 FROM lectures WHERE files_dir = ?",
                (row["version_dir"],),
            ).fetchone()
            slides_target = settings.data_dir / "import" / str(row["id"]) / "slides"
            if version_dir.exists() and not in_use:
                slides_target.parent.mkdir(parents=True, exist_ok=True)
                if (version_dir / "slides").exists():
                    shutil.move(str(version_dir / "slides"), str(slides_target))
                if version_dir.exists():
                    version_dir.rmdir()
                conn.execute(
                    "UPDATE imports SET status = 'ready', version_dir = NULL WHERE id = ?",
                    (row["id"],),
                )
    finally:
        conn.close()
