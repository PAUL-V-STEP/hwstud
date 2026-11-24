from __future__ import annotations

import html
import json
import re
from typing import Any

from ..config import Settings
from ..db import connect
from .lecture_import import normalize_title


def _variants(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return entry.setdefault("variants", [])


def collect_term_proposals(settings: Settings, import_id: int) -> list[dict[str, Any]]:
    conn = connect(settings)
    try:
        import_row = conn.execute(
            "SELECT report_json FROM imports WHERE id = ?", (import_id,)
        ).fetchone()
        if not import_row:
            return []
        report = json.loads(import_row["report_json"] or "{}")
        candidates: dict[str, dict[str, Any]] = {}
        jobs = conn.execute(
            "SELECT payload_json FROM jobs "
            "WHERE json_extract(payload_json, '$.import_id') = ? "
            "AND kind IN ('parse_abbr', 'abbr_guess')",
            (import_id,),
        ).fetchall()
        for job in jobs:
            payload = json.loads(job["payload_json"])
            for candidate in payload.get("items", []):
                candidates[normalize_title(candidate["term"])] = candidate
        existing = conn.execute("SELECT * FROM terms").fetchall()
    finally:
        conn.close()

    existing_by_key = {normalize_title(row["term"]): row for row in existing}
    merged: dict[str, dict[str, Any]] = {}

    def add(item: dict[str, Any], slide_n: int | None = None) -> None:
        term = item.get("term", "").strip()
        expansion = item.get("expansion", "").strip()
        if not term:
            return
        key = normalize_title(term)
        entry = merged.setdefault(
            key,
            {"key": key, "term": term, "variants": [], "slide_numbers": []},
        )
        variant = {
            "expansion": expansion,
            "english": item.get("english", "") or "",
        }
        if not any(
            normalize_title(variant["expansion"]) == normalize_title(existing_variant["expansion"])
            and variant["english"] == existing_variant["english"]
            for existing_variant in entry["variants"]
        ):
            entry["variants"].append(variant)
        if slide_n and slide_n not in entry["slide_numbers"]:
            entry["slide_numbers"].append(slide_n)

    for item in report.get("abbreviations", []):
        add(item, item.get("slide_n"))
    for occurrence in report.get("term_occurrences", []):
        existing_row = existing_by_key.get(normalize_title(occurrence.get("term", "")))
        if not existing_row:
            continue
        known = {
            "term": existing_row["term"],
            "expansion": existing_row["expansion"],
            "english": existing_row["english"] or "",
        }
        for slide_n in occurrence.get("slide_numbers", []):
            add(known, slide_n)
    for job in jobs:
        payload = json.loads(job["payload_json"])
        for item in payload.get("result", {}).get("items", []):
            candidate = candidates.get(normalize_title(item.get("term", "")), {})
            slide_numbers = candidate.get("slide_numbers")
            if slide_numbers is None:
                slide_n = candidate.get("slide_n")
                slide_numbers = [slide_n] if slide_n is not None else []
            for slide_n in slide_numbers:
                add(item, slide_n)

    result = []
    for key, entry in merged.items():
        entry["term_id"] = None
        existing_row = existing_by_key.get(key)
        if existing_row:
            entry["term_id"] = existing_row["id"]
            same = all(
                normalize_title(variant["expansion"]) == normalize_title(existing_row["expansion"])
                for variant in entry["variants"]
            )
            entry["category"] = "existing" if same else "conflict"
            entry["current"] = dict(existing_row)
        elif len({normalize_title(v["expansion"]) for v in entry["variants"]}) > 1:
            entry["category"] = "conflict"
        else:
            entry["category"] = "new"
        entry["requires_review"] = (
            entry["category"] == "conflict"
            or len(entry["variants"]) != 1
            or any(not variant["expansion"].strip() for variant in entry["variants"])
        )
        result.append(entry)
    return sorted(result, key=lambda item: item["term"].lower())


def apply_terms(conn, lecture_id: int, approved: list[dict[str, Any]]) -> None:
    for item in approved:
        term = item["term"].strip()
        expansion = item["expansion"].strip()
        english = item.get("english", "").strip()
        if not term or not expansion:
            continue
        rows = conn.execute("SELECT * FROM terms").fetchall()
        existing = next(
            (row for row in rows if normalize_title(row["term"]) == normalize_title(term)),
            None,
        )
        if existing:
            term_id = existing["id"]
            conn.execute(
                "UPDATE terms SET term = ?, expansion = ?, english = ?, status = 'verified', "
                "unused = 0 WHERE id = ?",
                (term, expansion, english, term_id),
            )
        else:
            cursor = conn.execute(
                "INSERT INTO terms(term, expansion, english, status, unused) "
                "VALUES (?, ?, ?, 'verified', 0)",
                (term, expansion, english),
            )
            term_id = int(cursor.lastrowid)
        for slide_n in item.get("slide_numbers", []):
            conn.execute(
                "INSERT OR IGNORE INTO term_refs(term_id, lecture_id, slide_n) VALUES (?, ?, ?)",
                (term_id, lecture_id, slide_n),
            )
        conn.execute(
            "DELETE FROM term_conflicts WHERE term_id = ? AND lecture_id = ?",
            (term_id, lecture_id),
        )


def visible_terms(conn, actor, *, lecture_slug: str | None = None, query: str = ""):
    parameters: list[Any] = []
    sql = (
        "SELECT DISTINCT t.*, l.slug, l.title AS lecture_title "
        "FROM terms t JOIN term_refs tr ON tr.term_id = t.id "
        "JOIN lectures l ON l.id = tr.lecture_id"
    )
    sql += " WHERE t.status = 'verified'"
    if not actor.is_admin:
        sql += (
            " AND l.status = 'open' AND ("
            "NOT EXISTS (SELECT 1 FROM lecture_groups lg WHERE lg.lecture_id = l.id) "
            "OR EXISTS (SELECT 1 FROM lecture_groups lg WHERE lg.lecture_id = l.id "
            "AND lg.group_id = ?))"
        )
        parameters.append(actor.group_id)
    else:
        sql += ""
    if lecture_slug:
        sql += " AND l.slug = ?"
        parameters.append(lecture_slug)
    if query:
        sql += " AND (lower(t.term) LIKE ? OR lower(t.expansion) LIKE ? OR lower(t.english) LIKE ?)"
        pattern = f"%{query.lower()}%"
        parameters.extend((pattern, pattern, pattern))
    sql += " ORDER BY lower(t.term), t.id"
    return conn.execute(sql, parameters).fetchall()


def highlight_terms(notes_html: str, terms) -> str:
    mapping = {
        row["term"]: row["expansion"]
        for row in terms
        if row["term"] and row["expansion"]
    }
    if not mapping:
        return notes_html
    pattern = re.compile(
        "(?<![\\w])(" + "|".join(re.escape(term) for term in sorted(mapping, key=len, reverse=True)) + ")(?![\\w])",
        re.IGNORECASE,
    )

    def replace(match: re.Match) -> str:
        term = match.group(1)
        expansion = next(
            (value for key, value in mapping.items() if key.lower() == term.lower()),
            "",
        )
        return (
            '<abbr class="term" title="'
            + html.escape(expansion, quote=True)
            + '">'
            + html.escape(term)
            + "</abbr>"
        )

    parts = re.split(r"(<[^>]+>)", notes_html)
    return "".join(part if part.startswith("<") else pattern.sub(replace, part) for part in parts)


def powerpoint_block(terms) -> str:
    values = []
    seen = set()
    for row in terms:
        key = normalize_title(row["term"])
        if key in seen or not row["expansion"]:
            continue
        seen.add(key)
        value = f"{row['term']} — {row['expansion']}"
        if row["english"]:
            value += f", {row['english']}"
        values.append(value)
    return "Сокращения: " + "; ".join(values) if values else "Сокращения:"
