from __future__ import annotations

import json

from app.db import connect
from app.services import lecture_import
from app.services.lecture_store import save_import

from .conftest import csrf_from, login_admin, login_student
from .lecture_helpers import create_lecture_import


def _saved_lecture(settings):
    import_id = create_lecture_import(settings)
    lecture_import.process_import(settings, import_id)
    return save_import(settings, import_id, "Process Analysis")


def test_upload_form_creates_background_import(client, settings, tmp_path):
    from .lecture_helpers import create_lecture_import
    source_id = create_lecture_import(settings)
    folder = settings.data_dir / "import" / str(source_id)
    login_admin(client)
    response = client.get("/admin/lectures")
    with (folder / "source.pptx").open("rb") as pptx, (folder / "source.pdf").open("rb") as pdf:
        response = client.post(
            "/admin/lectures/import",
            data={"csrf": csrf_from(response)},
            files={
                "pptx": ("source.pptx", pptx, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
                "pdf": ("source.pdf", pdf, "application/pdf"),
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert "/admin/lectures/import/" in response.headers["location"]


def test_student_access_images_and_exam_closure(client, settings):
    lecture_id, slug, _ = _saved_lecture(settings)
    login_student(client, "lecture_student", 1)
    assert client.get(f"/lectures/{slug}").status_code == 404
    conn = connect(settings)
    try:
        conn.execute("UPDATE lectures SET status = 'open' WHERE id = ?", (lecture_id,))
    finally:
        conn.close()
    response = client.get(f"/lectures/{slug}")
    assert response.status_code == 200
    assert "Process Analysis" in response.text
    assert 'class="lecture-page"' in response.text
    image = client.get(f"/l/{slug}/s/1.webp")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/webp"
    assert image.headers["cache-control"] == "private, max-age=3600"
    conn = connect(settings)
    try:
        conn.execute("UPDATE exam SET enabled = 1 WHERE id = 1")
    finally:
        conn.close()
    response = client.get(f"/lectures/{slug}")
    assert response.status_code == 200
    assert "Доступ к учебным материалам закрыт" in response.text
    assert "content-closed-640.jpg" in response.text
    assert client.get(f"/l/{slug}/s/1.webp").status_code == 404


def test_admin_can_view_hidden_in_student_template(client, settings):
    _id, slug, _ = _saved_lecture(settings)
    login_admin(client)
    response = client.get(f"/lectures/{slug}")
    assert response.status_code == 200
    assert "Слайд 1 / 2" in response.text


def test_lecture_header_lines_render_as_text_and_hide_empty_json(client, settings):
    _lecture_id, slug, _ = _saved_lecture(settings)
    login_admin(client)
    urls = (f"/lectures/{slug}", f"/lectures/{slug}/conspectus", f"/lectures/{slug}/s/1")
    for url in urls:
        page = client.get(url)
        assert page.status_code == 200
        assert 'class="lecture-note"' not in page.text
        assert "[]" not in page.text
    conn = connect(settings)
    try:
        conn.execute(
            "UPDATE lectures SET header_lines = ? WHERE slug = ?",
            (json.dumps(["Данные условные", "Общая строка"], ensure_ascii=False), slug),
        )
    finally:
        conn.close()
    for url in urls:
        page = client.get(url)
        assert "Данные условные · Общая строка" in page.text
        assert '[&quot;' not in page.text
        assert "[]" not in page.text


def test_access_form_updates_groups_and_announces(client, settings):
    lecture_id, slug, _ = _saved_lecture(settings)
    login_admin(client)
    response = client.get("/admin/lectures")
    response = client.post(
        f"/admin/lectures/{lecture_id}/access",
        data={
            "csrf": csrf_from(response),
            "title": "Process Analysis",
            "status": "open",
            "planned_date": "2026-12-01",
            "announce": "1",
            "selected_groups": ["2"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        lecture = conn.execute("SELECT * FROM lectures WHERE id = ?", (lecture_id,)).fetchone()
        assert lecture["status"] == "open"
        assert lecture["planned_date"] == "2026-12-01"
        assert conn.execute("SELECT group_id FROM lecture_groups WHERE lecture_id = ?", (lecture_id,)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'lecture_access'").fetchone()[0] == 1
    finally:
        conn.close()


def test_access_form_renames_lecture_without_changing_url_or_content(client, settings):
    lecture_id, slug, _ = _saved_lecture(settings)
    conn = connect(settings)
    try:
        files_dir = conn.execute(
            "SELECT files_dir FROM lectures WHERE id = ?", (lecture_id,)
        ).fetchone()[0]
        slide_count = conn.execute(
            "SELECT COUNT(*) FROM slides WHERE lecture_id = ?", (lecture_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    login_admin(client)
    response = client.get("/admin/lectures")
    assert 'name="title" value="Process Analysis"' in response.text
    response = client.post(
        f"/admin/lectures/{lecture_id}/access",
        data={
            "csrf": csrf_from(response),
            "title": "  Renamed Process  ",
            "status": "hidden",
            "planned_date": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(settings)
    try:
        lecture = conn.execute(
            "SELECT * FROM lectures WHERE id = ?", (lecture_id,)
        ).fetchone()
        assert lecture["title"] == "Renamed Process"
        assert lecture["title_norm"] == "renamed process"
        assert lecture["slug"] == slug
        assert lecture["files_dir"] == files_dir
        assert conn.execute(
            "SELECT COUNT(*) FROM slides WHERE lecture_id = ?", (lecture_id,)
        ).fetchone()[0] == slide_count
    finally:
        conn.close()
    page = client.get(f"/lectures/{slug}")
    assert page.status_code == 200
    assert "Renamed Process" in page.text


def test_access_form_rejects_duplicate_normalized_title(client, settings):
    lecture_id, _slug, _ = _saved_lecture(settings)
    second = create_lecture_import(
        settings,
        titles=("Other Lecture",),
        notes=("Overview\nDifferent content.",),
    )
    lecture_import.process_import(settings, second)
    save_import(settings, second, "Other Lecture")
    login_admin(client)
    response = client.get("/admin/lectures")
    response = client.post(
        f"/admin/lectures/{lecture_id}/access",
        data={
            "csrf": csrf_from(response),
            "title": "  other lecture ",
            "status": "hidden",
            "planned_date": "",
        },
    )
    assert response.status_code == 200
    assert "Лекция с таким названием уже существует" in response.text
    conn = connect(settings)
    try:
        assert conn.execute(
            "SELECT title FROM lectures WHERE id = ?", (lecture_id,)
        ).fetchone()[0] == "Process Analysis"
        assert conn.execute("SELECT COUNT(*) FROM lectures").fetchone()[0] == 2
    finally:
        conn.close()
