from __future__ import annotations

import secrets
import threading
import time
from typing import Callable

CARD_TTL_SECONDS = 600


def access_card_text(base_url: str, nick: str, key: str) -> str:
    return (
        f"Сайт курса: {base_url}/login\n"
        f"Ник: {nick}\n"
        f"Ключ: {key}\n\n"
        "Ключ показывается в течение 10 минут. Сохраните его в заметках или менеджере паролей.\n"
        "Ключ можно вводить с пробелами или дефисами, большими или маленькими буквами.\n"
        "Если потеряете ключ, напишите преподавателю: он выдаст новый."
    )


class AccessCardStore:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        nick: str,
        key: str,
        audience: str,
        student_id: int | None = None,
        next_path: str = "/",
    ) -> str:
        if audience not in {"student", "admin"}:
            raise ValueError("Неизвестный владелец карточки")
        card_id = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            self._items[card_id] = {
                "id": card_id,
                "nick": nick,
                "key": key,
                "audience": audience,
                "student_id": student_id,
                "next_path": next_path,
                "expires_at": self.clock() + CARD_TTL_SECONDS,
            }
        return card_id

    def get(self, card_id: str, actor) -> dict | None:
        with self._lock:
            self._purge()
            card = self._items.get(card_id)
            if not card or not self._allowed(card, actor):
                return None
            return dict(card)

    def delete(self, card_id: str, actor) -> bool:
        with self._lock:
            self._purge()
            card = self._items.get(card_id)
            if not card or not self._allowed(card, actor):
                return False
            del self._items[card_id]
            return True

    def _allowed(self, card: dict, actor) -> bool:
        if not actor:
            return False
        if card["audience"] == "admin":
            return bool(actor.is_admin)
        return (
            not actor.is_admin
            and card["student_id"] is not None
            and actor.student_id == card["student_id"]
        )

    def _purge(self) -> None:
        now = self.clock()
        expired = [key for key, value in self._items.items() if value["expires_at"] <= now]
        for key in expired:
            del self._items[key]
