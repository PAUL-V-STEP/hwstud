from __future__ import annotations

from pathlib import Path

from app.db import connect
from app.services import lecture_import
from app.services.lecture_store import save_import

from .lecture_helpers import create_lecture_import, import_row


def test_save_creates_hidden_lecture_and_deletes_sources(settings):
    import_id = create_lecture_import(settings)
    lecture_import.process_import(settings, import_id)
    lecture_id, slug, updated = save_import(settings, import_id, "Process Analysis")
    assert not updated
    assert slug == "process-analysis"
    assert import_row(settings, import_id)["status"] == "saved"
    import_dir = settings.data_dir / "import" / str(import_id)
    assert not import_dir.exists()
    conn = connect(settings)
    try:
        lecture = conn.execute("SELECT * FROM lectures WHERE id = ?", (lecture_id,)).fetchone()
        assert lecture["status"] == "hidden"
        assert conn.execute("SELECT COUNT(*) FROM slides WHERE lecture_id = ?", (lecture_id,)).fetchone()[0] == 2
        version = settings.base_dir / lecture["files_dir"] / "slides" / "1.webp"
        assert version.is_file()
    finally:
        conn.close()


def test_second_save_updates_same_lecture_and_replaces_version(settings):
    first = create_lecture_import(settings)
    lecture_import.process_import(settings, first)
    lecture_id, slug, _ = save_import(settings, first, "Process Analysis")
    conn = connect(settings)
    try:
        old_dir = conn.execute("SELECT files_dir FROM lectures WHERE id = ?", (lecture_id,)).fetchone()[0]
    finally:
        conn.close()
    second = create_lecture_import(
        settings,
        titles=("Process Analysis",),
        notes=("Updated overview\nThe new version replaces the old one.",),
    )
    lecture_import.process_import(settings, second)
    same_id, same_slug, updated = save_import(settings, second, "Process Analysis")
    assert same_id == lecture_id
    assert same_slug == slug
    assert updated
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM lectures").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM slides WHERE lecture_id = ?", (lecture_id,)).fetchone()[0] == 1
    finally:
        conn.close()
    assert not (settings.base_dir / old_dir).exists()


def test_recovery_after_move_before_commit_allows_save(settings):
    import_id = create_lecture_import(settings)
    lecture_import.process_import(settings, import_id)
    slug = "process-analysis"
    version_dir = settings.data_dir / "lectures" / slug / f"v{import_id}"
    version_dir.parent.mkdir(parents=True)
    import_dir = settings.data_dir / "import" / str(import_id)
    version_dir.mkdir()
    (import_dir / "slides").replace(version_dir / "slides")
    relative = str(version_dir.resolve())
    conn = connect(settings)
    try:
        conn.execute(
            "UPDATE imports SET status = 'saving', version_dir = ? WHERE id = ?",
            (relative, import_id),
        )
    finally:
        conn.close()
    lecture_import.recover_saving_imports(settings)
    assert import_row(settings, import_id)["status"] == "ready"
    assert (import_dir / "slides" / "1.webp").is_file()
    lecture_id, saved_slug, _ = save_import(settings, import_id, "Process Analysis")
    assert saved_slug == slug
    assert lecture_id > 0


def test_cancel_deletes_originals(settings):
    import_id = create_lecture_import(settings)
    lecture_import.process_import(settings, import_id)
    from app.services.lecture_store import cancel_import
    cancel_import(settings, import_id)
    assert import_row(settings, import_id)["status"] == "cancelled"
    assert not (settings.data_dir / "import" / str(import_id)).exists()
