from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from ..db import parse_utc, transaction, utc_now, utc_now_iso

KEY_WORDS_PATH = Path(__file__).resolve().parent.parent / "data" / "key_words_ru.txt"
KEY_WORD_RE = re.compile(r"^[а-ик-щыьэюя]{3,8}$")
KEY_FAILED_LIMIT = 5
KEY_LOCK_MINUTES = 15

_KEYBOARD_LAYOUT = str.maketrans({
    "q": "й", "w": "ц", "e": "у", "r": "к", "t": "е", "y": "н",
    "u": "г", "i": "ш", "o": "щ", "p": "з", "[": "х", "]": "ъ",
    "a": "ф", "s": "ы", "d": "в", "f": "а", "g": "п", "h": "р",
    "j": "о", "k": "л", "l": "д", ";": "ж", "'": "э",
    "z": "я", "x": "ч", "c": "с", "v": "м", "b": "и", "n": "т",
    "m": "ь", ",": "б", ".": "ю", "`": "ё",
})


@lru_cache(maxsize=4)
def _load_words(path_text: str) -> tuple[str, ...]:
    path = Path(path_text)
    words = path.read_text(encoding="utf-8").splitlines()
    if len(words) != 2048 or len(set(words)) != 2048:
        raise RuntimeError("Словарь ключей должен содержать ровно 2048 уникальных слова")
    if any(not KEY_WORD_RE.fullmatch(word) for word in words):
        raise RuntimeError("Словарь ключей содержит слово вне допустимого формата")
    return tuple(words)


def load_key_words(path: Path | None = None) -> tuple[str, ...]:
    return _load_words(str((path or KEY_WORDS_PATH).resolve()))


def generate_key(*, words_path: Path | None = None) -> str:
    words = load_key_words(words_path)
    return "-".join(secrets.choice(words) for _ in range(3))


def normalize_key(value: str, *, words_path: Path | None = None) -> str:
    text = value.strip().lower()
    if re.search(r"[a-z]", text):
        text = text.translate(_KEYBOARD_LAYOUT)
    text = text.replace("ё", "е").replace("й", "и")
    words = re.split(r"[\s_\-+]+", text.strip())
    dictionary = load_key_words(words_path)
    if len(words) != 3 or any(word not in dictionary for word in words):
        raise ValueError("Ник или ключ не подходят")
    return "-".join(words)


def hash_key(value: str, *, words_path: Path | None = None) -> str:
    from ..auth import hash_password

    return hash_password(normalize_key(value, words_path=words_path))


def verify_key(value: str, encoded: str, *, words_path: Path | None = None) -> bool:
    from ..auth import verify_password

    try:
        normalized = normalize_key(value, words_path=words_path)
    except ValueError:
        return False
    return verify_password(normalized, encoded)


def issue_key(conn, student_id: int, issued_by: str) -> str:
    if issued_by not in {"student", "admin", "transition"}:
        raise ValueError("Неизвестный источник выдачи ключа")
    key = generate_key()
    encoded = hash_key(key)
    now = utc_now_iso()
    with transaction(conn, immediate=True):
        cursor = conn.execute(
            "UPDATE students SET key_hash = ?, key_issued_at = ?, key_issued_by = ?, "
            "key_failed_count = 0, key_locked_until = NULL, login_token = ? WHERE id = ?",
            (encoded, now, issued_by, secrets.token_hex(16), student_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Студент не найден")
    return key


def check_student_key(
    conn, student, value: str, *, now: datetime | None = None
) -> str:
    current = now or utc_now()
    locked_until = parse_utc(student["key_locked_until"])
    if locked_until and locked_until > current:
        return "locked"
    if not student["key_hash"]:
        return "missing"
    if verify_key(value, student["key_hash"]):
        with transaction(conn, immediate=True):
            conn.execute(
                "UPDATE students SET key_failed_count = 0, key_locked_until = NULL, "
                "last_login_at = ? WHERE id = ?",
                (utc_now_iso(), student["id"]),
            )
        return "ok"
    failed = (
        1 if locked_until else int(student["key_failed_count"] or 0) + 1
    )
    locked = failed >= KEY_FAILED_LIMIT
    lock_until = (
        _iso(current + timedelta(minutes=KEY_LOCK_MINUTES)) if locked else None
    )
    with transaction(conn, immediate=True):
        conn.execute(
            "UPDATE students SET key_failed_count = ?, key_locked_until = ? WHERE id = ?",
            (failed, lock_until, student["id"]),
        )
        if locked:
            import json

            conn.execute(
                "INSERT INTO audit_log(at, actor, action, details_json) "
                "VALUES (?, 'system', 'student_key_locked', ?)",
                (utc_now_iso(), json.dumps({"failed": failed})),
            )
    return "locked" if locked else "invalid"


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")
