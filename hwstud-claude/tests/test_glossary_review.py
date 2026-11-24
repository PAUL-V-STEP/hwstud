from __future__ import annotations

import json

from app.db import connect, migrate, utc_now_iso
from app.services import glossary

from .conftest import csrf_from, login_admin


def create_ready_import(settings, report, candidates, results):
    migrate(settings)
    conn = connect(settings)
    try:
        cursor = conn.execute(
            "INSERT INTO imports(created_at, status, ready_at, report_json) VALUES (?, 'ready', ?, ?)",
            (utc_now_iso(), utc_now_iso(), json.dumps(report, ensure_ascii=False)),
        )
        import_id = int(cursor.lastrowid)
        if candidates:
            payload = {
                "import_id": import_id,
                "items": candidates,
                "result": {"items": results},
            }
            conn.execute(
                "INSERT INTO jobs(kind, priority, payload_json, status, run_after) "
                "VALUES ('abbr_guess', 4, ?, 'done', ?)",
                (json.dumps(payload, ensure_ascii=False), utc_now_iso()),
            )
        return import_id
    finally:
        conn.close()


def base_report():
    return {
        "title": "Review lecture", "title_norm": "review lecture", "slides": [],
        "sections": [], "header_lines": [], "warnings": [], "errors": [],
        "abbreviations": [], "complex_abbreviations": [], "sources": [], "citations": [],
        "gpt_candidate_count": 0,
    }


def test_only_unresolved_terms_are_shown_and_manual_expansion_saved(client, settings):
    report = base_report()
    report["gpt_candidate_count"] = 2
    candidates = [
        {"term": "AUTO", "context": "AUTO is known.", "slide_n": 1, "slide_numbers": [1, 3]},
        {"term": "OPEN", "context": "OPEN needs input.", "slide_n": 2},
    ]
    results = [
        {"term": "AUTO", "expansion": "Already clear", "english": ""},
        {"term": "OPEN", "expansion": "", "english": ""},
    ]
    import_id = create_ready_import(settings, report, candidates, results)
    proposals = glossary.collect_term_proposals(settings, import_id)
    assert not proposals[0]["requires_review"]
    assert proposals[1]["requires_review"]
    login_admin(client)
    response = client.get(f"/admin/lectures/import/{import_id}")
    assert "AUTO" not in response.text
    assert "OPEN" in response.text
    assert 'required=""' not in response.text
    response = client.post(
        f"/admin/lectures/import/{import_id}/confirm",
        data={
            "csrf": csrf_from(response), "terms": ["1"], "variant_1": "0",
            "expansion_1": "Entered manually", "english_1": "Open term",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        saved = json.loads(
            conn.execute("SELECT report_json FROM imports WHERE id = ?", (import_id,)).fetchone()[0]
        )
        assert saved["review_confirmed"] is True
        assert len(saved["approved_terms"]) == 2
        auto = next(item for item in saved["approved_terms"] if item["term"] == "AUTO")
        opened = next(item for item in saved["approved_terms"] if item["term"] == "OPEN")
        assert auto["expansion"] == "Already clear"
        assert auto["slide_numbers"] == [1, 3]
        assert opened["expansion"] == "Entered manually"
    finally:
        conn.close()


def test_known_term_occurrences_are_linked_without_gpt(settings):
    report = base_report()
    report["term_occurrences"] = [
        {"term": "KNOWN", "slide_numbers": [2, 4]},
    ]
    import_id = create_ready_import(settings, report, [], [])
    conn = connect(settings)
    try:
        conn.execute(
            "INSERT INTO terms(term, expansion, english, status) "
            "VALUES ('KNOWN', 'Known expansion', '', 'verified')"
        )
    finally:
        conn.close()
    proposals = glossary.collect_term_proposals(settings, import_id)
    assert len(proposals) == 1
    assert proposals[0]["term"] == "KNOWN"
    assert proposals[0]["slide_numbers"] == [2, 4]
    assert proposals[0]["category"] == "existing"
    assert not proposals[0]["requires_review"]


def test_selected_variant_is_used_even_without_javascript(client, settings):
    report = base_report()
    report["abbreviations"] = [
        {"term": "DUP", "expansion": "First variant", "english": "", "slide_n": 1},
        {"term": "DUP", "expansion": "Second variant", "english": "Second", "slide_n": 2},
    ]
    import_id = create_ready_import(settings, report, [], [])
    proposals = glossary.collect_term_proposals(settings, import_id)
    assert len(proposals) == 1
    assert proposals[0]["category"] == "conflict"
    login_admin(client)
    response = client.get(f"/admin/lectures/import/{import_id}")
    # Эмулируем отключённый JS: текстовое поле осталось с первым вариантом.
    response = client.post(
        f"/admin/lectures/import/{import_id}/confirm",
        data={
            "csrf": csrf_from(response), "terms": ["0"], "variant_0": "1",
            "expansion_0": "First variant", "english_0": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        saved = json.loads(
            conn.execute("SELECT report_json FROM imports WHERE id = ?", (import_id,)).fetchone()[0]
        )
        assert saved["approved_terms"][0]["expansion"] == "Second variant"
        assert saved["approved_terms"][0]["english"] == "Second"
    finally:
        conn.close()


def test_empty_unresolved_expansion_is_not_silently_skipped(client, settings):
    report = base_report()
    report["gpt_candidate_count"] = 1
    import_id = create_ready_import(
        settings,
        report,
        [{"term": "EMPTY", "context": "EMPTY needs input.", "slide_n": 1}],
        [{"term": "EMPTY", "expansion": "", "english": ""}],
    )
    login_admin(client)
    response = client.get(f"/admin/lectures/import/{import_id}")
    response = client.post(
        f"/admin/lectures/import/{import_id}/confirm",
        data={"csrf": csrf_from(response), "terms": ["0"], "variant_0": "0", "expansion_0": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        saved = json.loads(
            conn.execute("SELECT report_json FROM imports WHERE id = ?", (import_id,)).fetchone()[0]
        )
        assert not saved.get("review_confirmed", False)
    finally:
        conn.close()
