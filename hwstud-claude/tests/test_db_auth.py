from __future__ import annotations

from app.auth import create_student, normalize_nick, validate_nick
from app.db import connect, migrate, utc_now_iso


def test_migration_creates_required_seed_and_pragmas(settings):
    assert migrate(settings) == [1, 2, 3, 4, 5, 6]
    assert migrate(settings) == []
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 5
        assert conn.execute(
            "SELECT value FROM settings WHERE key = 'assignments_count'"
        ).fetchone()[0] == "5"
        assert [row[0] for row in conn.execute(
            "SELECT position FROM assignments ORDER BY position"
        )] == [1, 2, 3, 4, 5]
        assert conn.execute("SELECT title FROM assignments WHERE id = 5").fetchone()[0] == "Общее задание по всему курсу"
        instructions = conn.execute(
            "SELECT value FROM settings WHERE key = 'grading_instructions'"
        ).fetchone()[0]
        assert "преподаватель университета" in instructions
        assert "первого курса магистратуры" in instructions
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_fifth_assignment_migration_preserves_existing_submission(settings):
    conn = connect(settings)
    try:
        conn.execute(
            "CREATE TABLE schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in (1, 2, 3):
            sql = (settings.base_dir / "migrations" / next(
                path.name for path in (settings.base_dir / "migrations").glob("*.sql")
                if path.name.startswith(f"{version:03d}_")
            )).read_text(encoding="utf-8")
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (version, utc_now_iso()),
            )
        student, error = create_student(conn, "legacy_student", 1)
        assert not error
        conn.execute(
            "INSERT INTO submissions(student_id, assignment_id, attempt, file, mime, size, "
            "created_at, status) VALUES (?, 1, 1, 'legacy.txt', 'text/plain', 4, ?, 'queued')",
            (student["id"], utc_now_iso()),
        )
    finally:
        conn.close()
    assert migrate(settings) == [4, 5, 6]
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 5
        assert conn.execute(
            "SELECT file FROM submissions WHERE student_id = ?", (student["id"],)
        ).fetchone()[0] == "legacy.txt"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_nick_rules():
    assert normalize_nick("  Ivan_2 ") == "ivan_2"
    assert validate_nick("Abc-123_x") is None
    for nick in ("ab", "a" * 25, "Иван", "two words", "nick!"):
        assert validate_nick(nick)


def test_group_capacity_and_enrollment(settings):
    migrate(settings)
    conn = connect(settings)
    try:
        for index in range(20):
            row, error = create_student(conn, f"student_{index}", 1)
            assert row is not None
            assert error is None
        row, error = create_student(conn, "student_20", 1)
        assert row is None
        assert error == "В группе уже 20 участников."
        conn.execute("UPDATE groups SET enroll_open = 0 WHERE id = 2")
        row, error = create_student(conn, "other_group", 2)
        assert row is None
        assert error == "Набор в эту группу закрыт."
    finally:
        conn.close()
