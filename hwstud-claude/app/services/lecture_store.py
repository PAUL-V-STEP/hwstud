from __future__ import annotations

import json
import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import connect, parse_utc, transaction, utc_now, utc_now_iso
from .lecture_import import load_report, normalize_title, slugify
from . import glossary, literature


def _relative(path: Path, settings: Settings) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(settings.base_dir.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _absolute(value: str, settings: Settings) -> Path:
    path = Path(value)
    return path if path.is_absolute() else settings.base_dir / path


def _safe_remove(path: Path, root: Path) -> None:
    resolved = path.resolve()
    root = root.resolve()
    if resolved != root and resolved.is_relative_to(root) and resolved.exists():
        shutil.rmtree(resolved)


def save_import(
    settings: Settings, import_id: int, title: str
) -> tuple[int, str, bool]:
    title = title.strip()
    if not title:
        raise ValueError("Введите название лекции")
    title_norm = normalize_title(title)
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT * FROM imports WHERE id = ?", (import_id,)
        ).fetchone()
        if not row or row["status"] != "ready":
            raise ValueError("Предпросмотр ещё не готов или уже недоступен")
        ready_at = parse_utc(row["ready_at"])
        if ready_at and utc_now() - ready_at > timedelta(days=7):
            conn.execute(
                "UPDATE imports SET status = 'expired' WHERE id = ?", (import_id,)
            )
            raise ValueError("Срок предпросмотра истёк. Загрузите лекцию заново")
        report = load_report(row)
        needs_review = bool(
            report.get("abbreviations")
            or report.get("complex_abbreviations")
            or report.get("gpt_candidate_count")
            or report.get("term_occurrences")
            or report.get("sources")
        )
        if needs_review and not report.get("review_confirmed"):
            raise ValueError("Подтвердите состав глоссария и литературы перед сохранением")
        source_metadata = literature.collect_source_meta(settings, import_id)
        target = conn.execute(
            "SELECT * FROM lectures WHERE title_norm = ?", (title_norm,)
        ).fetchone()
        slug = target["slug"] if target else _new_slug(conn, title)
        version_dir = settings.data_dir / "lectures" / slug / f"v{import_id}"
        version_relative = _relative(version_dir, settings)
        with transaction(conn, immediate=True):
            cursor = conn.execute(
                "UPDATE imports SET status = 'saving', version_dir = ?, "
                "target_lecture_id = ? WHERE id = ? AND status = 'ready'",
                (version_relative, target["id"] if target else None, import_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Сохранение уже началось в другой вкладке")
        old_files_dir = target["files_dir"] if target else None
    finally:
        conn.close()

    import_dir = settings.data_dir / "import" / str(import_id)
    version_dir.parent.mkdir(parents=True, exist_ok=True)
    if version_dir.exists():
        raise RuntimeError("Папка версии уже существует; требуется восстановление")
    version_dir.mkdir(parents=True)
    os.replace(import_dir / "slides", version_dir / "slides")

    conn = connect(settings)
    lecture_id = target["id"] if target else None
    try:
        now = utc_now_iso()
        with transaction(conn, immediate=True):
            if target:
                conn.execute(
                    "UPDATE lectures SET title = ?, title_norm = ?, header_lines = ?, "
                    "files_dir = ?, updated_at = ? WHERE id = ?",
                    (
                        title,
                        title_norm,
                        json.dumps(report["header_lines"], ensure_ascii=False),
                        version_relative,
                        now,
                        lecture_id,
                    ),
                )
                conn.execute("DELETE FROM slides WHERE lecture_id = ?", (lecture_id,))
                conn.execute("DELETE FROM lecture_sections WHERE lecture_id = ?", (lecture_id,))
            else:
                number = conn.execute(
                    "SELECT COALESCE(MAX(num), 0) + 1 FROM lectures"
                ).fetchone()[0]
                cursor = conn.execute(
                    "INSERT INTO lectures(slug, num, title, title_norm, status, "
                    "header_lines, files_dir, updated_at) "
                    "VALUES (?, ?, ?, ?, 'hidden', ?, ?, ?)",
                    (
                        slug,
                        number,
                        title,
                        title_norm,
                        json.dumps(report["header_lines"], ensure_ascii=False),
                        version_relative,
                        now,
                    ),
                )
                lecture_id = int(cursor.lastrowid)
            for section in report["sections"]:
                conn.execute(
                    "INSERT INTO lecture_sections(lecture_id, idx, title, first_slide) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        lecture_id,
                        section["idx"],
                        section["title"],
                        section["first_slide"],
                    ),
                )
            for slide in report["slides"]:
                number = slide["n"]
                conn.execute(
                    "INSERT INTO slides(lecture_id, n, title, notes_html, img, thumb, "
                    "section_idx, missing_notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        lecture_id,
                        number,
                        slide["title"],
                        slide["notes_html"],
                        f"slides/{number}.webp",
                        f"slides/thumb/{number}.webp",
                        _section_for(report["sections"], number),
                        int(slide["missing_notes"]),
                    ),
                )
            if target:
                conn.execute("DELETE FROM term_refs WHERE lecture_id = ?", (lecture_id,))
                conn.execute("DELETE FROM source_refs WHERE lecture_id = ?", (lecture_id,))
                conn.execute("DELETE FROM term_conflicts WHERE lecture_id = ?", (lecture_id,))
            glossary.apply_terms(conn, lecture_id, report.get("approved_terms", []))
            literature.apply_sources(
                conn,
                lecture_id,
                report.get("approved_sources", []),
                report.get("citations", []),
                source_metadata,
            )
            conn.execute(
                "UPDATE terms SET unused = NOT EXISTS "
                "(SELECT 1 FROM term_refs WHERE term_id = terms.id)"
            )
            conn.execute(
                "UPDATE imports SET status = 'saved' WHERE id = ?", (import_id,)
            )
    finally:
        conn.close()

    if old_files_dir:
        _safe_remove(
            _absolute(old_files_dir, settings), settings.data_dir / "lectures"
        )
    _safe_remove(import_dir, settings.data_dir / "import")
    return int(lecture_id), slug, bool(target)


def _new_slug(conn, title: str) -> str:
    base = slugify(title)
    candidate = base
    suffix = 2
    while conn.execute(
        "SELECT 1 FROM lectures WHERE slug = ?", (candidate,)
    ).fetchone():
        candidate = f"{base[:56]}-{suffix}"
        suffix += 1
    return candidate


def _section_for(sections: list[dict[str, Any]], slide: int) -> int | None:
    current = None
    for section in sorted(sections, key=lambda item: item["first_slide"]):
        if section["first_slide"] <= slide:
            current = section["idx"]
    return current


def cancel_import(settings: Settings, import_id: int) -> None:
    conn = connect(settings)
    try:
        row = conn.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
        if not row:
            raise ValueError("Импорт не найден")
        if row["status"] in {"processing", "saving"}:
            raise ValueError("Нельзя отменить импорт во время разбора или сохранения")
        with transaction(conn, immediate=True):
            conn.execute(
                "UPDATE imports SET status = 'cancelled' WHERE id = ?", (import_id,)
            )
            conn.execute(
                "UPDATE jobs SET status = 'cancelled' "
                "WHERE status IN ('queued', 'paused_no_funds') "
                "AND json_extract(payload_json, '$.import_id') = ?",
                (import_id,),
            )
    finally:
        conn.close()
    _safe_remove(
        settings.data_dir / "import" / str(import_id),
        settings.data_dir / "import",
    )
