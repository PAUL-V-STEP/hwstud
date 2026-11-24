from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from ..config import Settings
from ..db import connect

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
NUMBER_RE = re.compile(r"\[(\d+)\]")


def _norm(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower().replace("ё", "е")
    return " ".join("".join(char if char.isalnum() else " " for char in value).split())


def extract_slide_sources(
    slide_title: str, visible_lines: list[str], slide_n: int
) -> tuple[list[dict[str, Any]], list[dict[str, int]]]:
    bibliography_slide = _norm(slide_title) in {"источники", "литература"}
    definitions = []
    citations = []
    for line in visible_lines:
        stripped = line.strip()
        if not stripped or _norm(stripped) in {"источники", "литература"}:
            continue
        numbered = re.match(r"^\[(\d+)\]\s*(.*)$", stripped)
        number = int(numbered.group(1)) if numbered else None
        body = numbered.group(2) if numbered else stripped
        url_match = URL_RE.search(body)
        is_definition = bool(url_match and (numbered or bibliography_slide)) or bibliography_slide
        if is_definition:
            url = url_match.group(0).rstrip(".,);") if url_match else ""
            text = URL_RE.sub("", body).strip(" .;—-")
            if ". " in text:
                author, title = text.split(". ", 1)
            else:
                author, title = "", text
            if not title:
                continue
            definitions.append(
                {
                    "slide_n": slide_n,
                    "local_number": number or len(definitions) + 1,
                    "author": author.strip(),
                    "title": title.strip(" ."),
                    "url": url,
                    "raw": stripped,
                }
            )
        else:
            citations.extend(
                {"slide_n": slide_n, "local_number": int(value)}
                for value in NUMBER_RE.findall(stripped)
            )
    return definitions, citations


def merge_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for source in sources:
        found = next(
            (
                item for item in merged
                if (source["url"] and item["url"] == source["url"])
                or (
                    source["author"]
                    and source["title"]
                    and _norm(item["author"]) == _norm(source["author"])
                    and _norm(item["title"]) == _norm(source["title"])
                )
            ),
            None,
        )
        if found:
            found["refs"].append(
                {"slide_n": source["slide_n"], "local_number": source["local_number"]}
            )
            continue
        item = dict(source)
        item["refs"] = [
            {"slide_n": source["slide_n"], "local_number": source["local_number"]}
        ]
        item["source_index"] = len(merged)
        merged.append(item)
    return merged


def collect_source_meta(settings: Settings, import_id: int) -> dict[int, dict[str, Any]]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM jobs "
            "WHERE json_extract(payload_json, '$.import_id') = ? AND kind = 'source_meta'",
            (import_id,),
        ).fetchall()
    finally:
        conn.close()
    result = {}
    for row in rows:
        payload = json.loads(row["payload_json"])
        if "result" in payload:
            result[int(payload["source_index"])] = payload["result"]
    return result


def build_source_review(settings: Settings, import_id: int) -> list[dict[str, Any]]:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT report_json FROM imports WHERE id = ?", (import_id,)
        ).fetchone()
        if not row:
            return []
        report = json.loads(row["report_json"] or "{}")
        existing = conn.execute("SELECT * FROM sources").fetchall()
    finally:
        conn.close()
    metadata = collect_source_meta(settings, import_id)
    review = []
    for source in report.get("sources", []):
        item = dict(source)
        item.update(metadata.get(source["source_index"], {}))
        match = next(
            (
                row for row in existing
                if (source["url"] and row["url"] == source["url"])
                or (
                    source["author"]
                    and source["title"]
                    and _norm(row["author"]) == _norm(source["author"])
                    and _norm(row["title"]) == _norm(source["title"])
                )
            ),
            None,
        )
        item["source_id"] = match["id"] if match else None
        if not match:
            item["category"] = "new"
        elif _norm(match["title"]) == _norm(source["title"]):
            item["category"] = "existing"
        else:
            item["category"] = "conflict"
        review.append(item)
    return review


def apply_sources(
    conn,
    lecture_id: int,
    approved: list[dict[str, Any]],
    citations: list[dict[str, int]],
    metadata: dict[int, dict[str, Any]],
) -> None:
    for item in approved:
        meta = metadata.get(item["source_index"], {})
        rows = conn.execute("SELECT * FROM sources").fetchall()
        existing = next(
            (
                row for row in rows
                if (item["url"] and row["url"] == item["url"])
                or (
                    item["author"]
                    and item["title"]
                    and _norm(row["author"]) == _norm(item["author"])
                    and _norm(row["title"]) == _norm(item["title"])
                )
            ),
            None,
        )
        if existing:
            source_id = existing["id"]
            conn.execute(
                "UPDATE sources SET author = ?, title = ?, url = ?, type = ?, "
                "annotation = ? WHERE id = ?",
                (
                    item["author"], item["title"], item["url"],
                    meta.get("type", existing["type"]),
                    meta.get("annotation", existing["annotation"]),
                    source_id,
                ),
            )
        else:
            cursor = conn.execute(
                "INSERT INTO sources(author, title, url, type, annotation, priority) "
                "VALUES (?, ?, ?, ?, ?, 'extra')",
                (
                    item["author"], item["title"], item["url"],
                    meta.get("type", ""), meta.get("annotation", ""),
                ),
            )
            source_id = int(cursor.lastrowid)
        refs = list(item.get("refs", []))
        known_numbers = {ref["local_number"] for ref in refs}
        refs.extend(
            citation for citation in citations
            if citation["local_number"] in known_numbers
            and citation not in refs
        )
        for ref in refs:
            conn.execute(
                "INSERT OR IGNORE INTO source_refs(source_id, lecture_id, slide_n, local_number) "
                "VALUES (?, ?, ?, ?)",
                (source_id, lecture_id, ref["slide_n"], ref["local_number"]),
            )


def visible_sources(
    conn,
    actor,
    *,
    lecture_slug: str | None = None,
    query: str = "",
    priority: str = "",
):
    parameters: list[Any] = []
    sql = (
        "SELECT DISTINCT s.*, l.slug, l.title AS lecture_title, "
        "GROUP_CONCAT(DISTINCT sr.slide_n) AS slide_numbers "
        "FROM sources s JOIN source_refs sr ON sr.source_id = s.id "
        "JOIN lectures l ON l.id = sr.lecture_id"
    )
    if not actor.is_admin:
        sql += (
            " WHERE l.status = 'open' AND ("
            "NOT EXISTS (SELECT 1 FROM lecture_groups lg WHERE lg.lecture_id = l.id) "
            "OR EXISTS (SELECT 1 FROM lecture_groups lg WHERE lg.lecture_id = l.id "
            "AND lg.group_id = ?))"
        )
        parameters.append(actor.group_id)
    else:
        sql += " WHERE 1 = 1"
    if lecture_slug:
        sql += " AND l.slug = ?"
        parameters.append(lecture_slug)
    if priority in {"main", "extra"}:
        sql += " AND s.priority = ?"
        parameters.append(priority)
    if query:
        pattern = f"%{query.lower()}%"
        sql += " AND (lower(s.author) LIKE ? OR lower(s.title) LIKE ? OR lower(s.annotation) LIKE ?)"
        parameters.extend((pattern, pattern, pattern))
    sql += " GROUP BY s.id, l.id ORDER BY l.num, lower(s.title)"
    return conn.execute(sql, parameters).fetchall()


def citation_links(notes_html: str, lecture_slug: str) -> str:
    parts = re.split(r"(<[^>]+>)", notes_html)
    for index, part in enumerate(parts):
        if part.startswith("<"):
            continue
        parts[index] = NUMBER_RE.sub(
            lambda match: (
                f'<a class="citation" href="/literature/{lecture_slug}/source/'
                f'{match.group(1)}">[{match.group(1)}]</a>'
            ),
            part,
        )
    return "".join(parts)
