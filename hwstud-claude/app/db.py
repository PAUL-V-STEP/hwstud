from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .config import Settings


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def connect(settings: Settings) -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        settings.db_path,
        timeout=5,
        isolation_level=None,
        check_same_thread=True,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def migrate(settings: Settings) -> list[int]:
    migrations_dir = settings.base_dir / "migrations"
    applied_now: list[int] = []
    conn = connect(settings)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            row["version"]
            for row in conn.execute("SELECT version FROM schema_version").fetchall()
        }
        for path in sorted(migrations_dir.glob("*.sql")):
            try:
                version = int(path.name.split("_", 1)[0])
            except (ValueError, IndexError) as exc:
                raise ValueError(f"Некорректное имя миграции: {path.name}") from exc
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            script = (
                "BEGIN IMMEDIATE;\n"
                + sql
                + "\nINSERT INTO schema_version(version, applied_at) VALUES ("
                + str(version)
                + ", '"
                + utc_now_iso()
                + "');\nCOMMIT;"
            )
            try:
                # Перестроение таблиц в миграциях требует временно отключить
                # внешние ключи до начала транзакции; обычные подключения не меняются.
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.executescript(script)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
            applied_now.append(version)
        return applied_now
    finally:
        conn.close()
