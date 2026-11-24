from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx

from ..config import Settings
from ..db import connect, parse_utc, transaction, utc_now, utc_now_iso

GPT_JOB_KINDS = ("grading", "parse_abbr", "abbr_guess", "source_meta")


@dataclass(slots=True)
class BalanceReport:
    ok: bool
    level: str
    balance: float | None = None
    paused: bool = False
    error: str = ""


def estimate_reserve(settings: Settings) -> float:
    conn = connect(settings)
    try:
        values = {
            row["key"]: Decimal(row["value"])
            for row in conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('reserve_factor', 'image_tokens', 'price_grading_in', 'price_grading_out')"
            )
        }
    finally:
        conn.close()
    tokens = Decimal(12_000 + 30_000) / 2 + 500 + 5 * values["image_tokens"]
    cost = (
        tokens * values["price_grading_in"]
        + 1500 * values["price_grading_out"]
    ) / Decimal(1_000_000)
    reserve = cost * values["reserve_factor"]
    return float(reserve.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


class BalanceService:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def check(self, *, source: str = "manual") -> BalanceReport:
        if source not in {"poll", "manual", "probe"}:
            raise ValueError("Неизвестный источник проверки баланса")
        if not self.settings.proxyapi_key:
            return self._configuration_error()
        try:
            async with httpx.AsyncClient(transport=self.transport) as client:
                response = await client.get(
                    self.settings.proxyapi_balance_url,
                    headers={
                        "Authorization": f"Bearer {self.settings.proxyapi_key}"
                    },
                    timeout=10,
                )
        except (httpx.NetworkError, httpx.TimeoutException) as exc:
            return self._request_error(f"Сеть: {type(exc).__name__}")

        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                return self._request_error("Некорректный JSON баланса")
            balance = _number(data.get("balance")) if isinstance(data, dict) else None
            if balance is None:
                return self._request_error("В ответе нет числового balance")
            budget = data.get("budget") if isinstance(data, dict) else None
            budget = budget if isinstance(budget, dict) else {}
            return self._success(
                balance=balance,
                budget_limit=_number(budget.get("limit")),
                budget_used=_number(budget.get("used")),
                source=source,
            )
        if response.status_code == 401:
            return self._key_rejected("Ключ ProxyAPI отклонён")
        if response.status_code == 403:
            return self._balance_denied()
        if response.status_code == 402:
            self.pause("ProxyAPI ответил 402: нет средств")
            return BalanceReport(False, "no_funds", paused=True, error="Нет средств")
        return self._request_error(f"HTTP {response.status_code}")

    def _configuration_error(self) -> BalanceReport:
        conn = connect(self.settings)
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE balance_state SET level = 'unknown', last_error = 'no_key' "
                    "WHERE id = 1"
                )
        finally:
            conn.close()
        return BalanceReport(False, "unknown", error="PROXYAPI_KEY не задан")

    def _request_error(self, error: str) -> BalanceReport:
        conn = connect(self.settings)
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE balance_state SET last_error = ? WHERE id = 1", (error,)
                )
                state = conn.execute(
                    "SELECT * FROM balance_state WHERE id = 1"
                ).fetchone()
                level = self._level(conn, state)
                conn.execute(
                    "UPDATE balance_state SET level = ? WHERE id = 1", (level,)
                )
                latest = conn.execute(
                    "SELECT balance FROM balance_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
        finally:
            conn.close()
        return BalanceReport(
            False,
            level,
            balance=latest["balance"] if latest else None,
            paused=bool(state["paused"]),
            error=error,
        )

    def _key_rejected(self, error: str) -> BalanceReport:
        conn = connect(self.settings)
        try:
            with transaction(conn, immediate=True):
                conn.execute(
                    "UPDATE balance_state SET level = 'no_funds', paused = 1, "
                    "paused_since = COALESCE(paused_since, ?), last_error = 'key_rejected', "
                    "gpt_stopped = 1 WHERE id = 1",
                    (utc_now_iso(),),
                )
                _pause_jobs(conn)
                latest = conn.execute(
                    "SELECT balance FROM balance_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
                self._maybe_alert(
                    conn, "unknown", "no_funds", latest["balance"] if latest else None
                )
        finally:
            conn.close()
        return BalanceReport(False, "no_funds", paused=True, error=error)

    def _balance_denied(self) -> BalanceReport:
        conn = connect(self.settings)
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE balance_state SET balance_access = 'denied', "
                    "last_error = 'balance_denied' WHERE id = 1"
                )
                state = conn.execute(
                    "SELECT * FROM balance_state WHERE id = 1"
                ).fetchone()
                level = "no_funds" if state["paused"] else "unknown"
                conn.execute(
                    "UPDATE balance_state SET level = ? WHERE id = 1", (level,)
                )
                latest = conn.execute(
                    "SELECT balance FROM balance_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
        finally:
            conn.close()
        return BalanceReport(
            False,
            level,
            balance=latest["balance"] if latest else None,
            paused=bool(state["paused"]),
            error="Разрешите ключу запрос баланса в кабинете ProxyAPI",
        )

    def _success(
        self,
        *,
        balance: float,
        budget_limit: float | None,
        budget_used: float | None,
        source: str,
    ) -> BalanceReport:
        now = utc_now_iso()
        conn = connect(self.settings)
        resumed = False
        try:
            with transaction(conn, immediate=True):
                conn.execute(
                    "INSERT INTO balance_log(at, balance, budget_limit, budget_used, source) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, balance, budget_limit, budget_used, source),
                )
                previous = conn.execute(
                    "SELECT * FROM balance_state WHERE id = 1"
                ).fetchone()
                conn.execute(
                    "UPDATE balance_state SET balance_access = 'allowed', last_ok_at = ?, "
                    "last_error = NULL, gpt_stopped = 0 WHERE id = 1",
                    (now,),
                )
                reserve = estimate_reserve(self.settings)
                should_pause = balance < reserve
                if should_pause and not previous["paused"]:
                    conn.execute(
                        "UPDATE balance_state SET paused = 1, paused_since = ? WHERE id = 1",
                        (now,),
                    )
                elif not should_pause and previous["paused"]:
                    conn.execute(
                        "UPDATE balance_state SET paused = 0, paused_since = NULL WHERE id = 1"
                    )
                    _resume_jobs(conn)
                    resumed = True
                if should_pause:
                    _pause_jobs(conn)
                state = conn.execute(
                    "SELECT * FROM balance_state WHERE id = 1"
                ).fetchone()
                level = self._level(conn, state, balance=balance)
                conn.execute(
                    "UPDATE balance_state SET level = ? WHERE id = 1", (level,)
                )
                self._maybe_alert(conn, previous["level"], level, balance)
        finally:
            conn.close()
        return BalanceReport(
            True,
            level,
            balance=balance,
            paused=should_pause,
            error="Баланс восстановлен; задачи возвращены в очередь" if resumed else "",
        )

    def _level(
        self,
        conn: sqlite3.Connection,
        state: sqlite3.Row,
        *,
        balance: float | None = None,
    ) -> str:
        if state["paused"]:
            return "no_funds"
        last_ok = parse_utc(state["last_ok_at"])
        if state["balance_access"] != "allowed" or not last_ok:
            return "unknown"
        if utc_now() - last_ok > timedelta(hours=2):
            return "unknown"
        if balance is None:
            row = conn.execute(
                "SELECT balance FROM balance_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            balance = row["balance"] if row else None
        if balance is None:
            return "unknown"
        values = {
            row["key"]: float(row["value"])
            for row in conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('balance_low', 'balance_critical')"
            )
        }
        if balance < estimate_reserve(self.settings):
            return "no_funds"
        if balance < values["balance_critical"]:
            return "critical"
        if balance < values["balance_low"]:
            return "low"
        return "ok"

    def pause(self, reason: str) -> None:
        conn = connect(self.settings)
        try:
            with transaction(conn, immediate=True):
                previous = conn.execute(
                    "SELECT level FROM balance_state WHERE id = 1"
                ).fetchone()
                conn.execute(
                    "UPDATE balance_state SET level = 'no_funds', paused = 1, "
                    "paused_since = COALESCE(paused_since, ?), last_error = ? WHERE id = 1",
                    (utc_now_iso(), reason),
                )
                _pause_jobs(conn)
                latest = conn.execute(
                    "SELECT balance FROM balance_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
                self._maybe_alert(
                    conn, previous["level"], "no_funds",
                    latest["balance"] if latest else None,
                )
        finally:
            conn.close()

    def reject_key(self, reason: str) -> None:
        self._key_rejected(reason)

    def resume_if_possible(self) -> bool:
        conn = connect(self.settings)
        try:
            with transaction(conn, immediate=True):
                state = conn.execute(
                    "SELECT * FROM balance_state WHERE id = 1"
                ).fetchone()
                latest = conn.execute(
                    "SELECT balance FROM balance_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if (
                    state["balance_access"] != "allowed"
                    or not latest
                    or latest["balance"] is None
                    or latest["balance"] < estimate_reserve(self.settings)
                ):
                    return False
                conn.execute(
                    "UPDATE balance_state SET paused = 0, paused_since = NULL, "
                    "gpt_stopped = 0, last_error = NULL WHERE id = 1"
                )
                _resume_jobs(conn)
                state = conn.execute(
                    "SELECT * FROM balance_state WHERE id = 1"
                ).fetchone()
                level = self._level(conn, state, balance=latest["balance"])
                conn.execute(
                    "UPDATE balance_state SET level = ? WHERE id = 1", (level,)
                )
                return True
        finally:
            conn.close()

    def _maybe_alert(
        self,
        conn: sqlite3.Connection,
        previous_level: str,
        level: str,
        balance: float | None,
    ) -> None:
        recovery = previous_level == "no_funds" and level != "no_funds"
        if level == "unknown" and not recovery:
            return
        key = "recovery" if recovery else level
        if key not in {"low", "critical", "no_funds", "recovery"}:
            return
        now = utc_now()
        row = conn.execute(
            "SELECT sent_at FROM balance_alerts WHERE level = ?", (key,)
        ).fetchone()
        if row and parse_utc(row["sent_at"]) and now - parse_utc(row["sent_at"]) < timedelta(hours=12):
            return
        if recovery:
            subject = "hwstud: баланс ProxyAPI — восстановлен"
        else:
            subject = f"hwstud: баланс ProxyAPI — {level}"
        counts = conn.execute(
            "SELECT SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued, "
            "SUM(CASE WHEN status = 'paused_no_funds' THEN 1 ELSE 0 END) AS paused FROM jobs"
        ).fetchone()
        body = (
            f"Уровень: {level}\nБаланс: {balance if balance is not None else 'неизвестен'}\n"
            f"В очереди: {counts['queued'] or 0}; на паузе: {counts['paused'] or 0}\n"
            f"Монитор: {self.settings.app_base_url}/admin/settings/proxyapi"
        )
        conn.execute(
            "INSERT INTO jobs(kind, priority, payload_json, run_after) "
            "VALUES ('mail', 1, ?, ?)",
            (json.dumps({"subject": subject, "body": body}, ensure_ascii=False), utc_now_iso()),
        )
        conn.execute(
            "INSERT INTO balance_alerts(level, sent_at) VALUES (?, ?) "
            "ON CONFLICT(level) DO UPDATE SET sent_at = excluded.sent_at",
            (key, utc_now_iso()),
        )

    def force_resume(self) -> None:
        conn = connect(self.settings)
        try:
            with transaction(conn, immediate=True):
                previous = conn.execute(
                    "SELECT * FROM balance_state WHERE id = 1"
                ).fetchone()
                conn.execute(
                    "UPDATE balance_state SET paused = 0, paused_since = NULL, "
                    "gpt_stopped = 0, last_error = NULL WHERE id = 1"
                )
                _resume_jobs(conn)
                state = conn.execute(
                    "SELECT * FROM balance_state WHERE id = 1"
                ).fetchone()
                level = self._level(conn, state)
                conn.execute(
                    "UPDATE balance_state SET level = ? WHERE id = 1", (level,)
                )
                latest = conn.execute(
                    "SELECT balance FROM balance_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
                self._maybe_alert(
                    conn,
                    previous["level"],
                    level,
                    latest["balance"] if latest else None,
                )
        finally:
            conn.close()

    def snapshot(self) -> dict[str, Any]:
        conn = connect(self.settings)
        try:
            state = conn.execute(
                "SELECT * FROM balance_state WHERE id = 1"
            ).fetchone()
            latest = conn.execute(
                "SELECT * FROM balance_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            counts = conn.execute(
                "SELECT "
                "SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued, "
                "SUM(CASE WHEN status = 'paused_no_funds' THEN 1 ELSE 0 END) AS paused "
                "FROM jobs"
            ).fetchone()
            cost_rows = conn.execute(
                "SELECT started_at, cost_est FROM jobs WHERE cost_est IS NOT NULL"
            ).fetchall()
            today = utc_now().astimezone(self.settings.timezone).date()
            expenses: dict[Any, float] = {}
            for row in cost_rows:
                started = parse_utc(row["started_at"])
                if started:
                    day = started.astimezone(self.settings.timezone).date()
                    expenses[day] = expenses.get(day, 0.0) + (row["cost_est"] or 0.0)
            expense_today = expenses.get(today, 0.0)
            expense_week = sum(
                value for day, value in expenses.items()
                if 0 <= (today - day).days <= 6
            )
            expense_month = sum(
                value for day, value in expenses.items()
                if day.year == today.year and day.month == today.month
            )
            recent_costs = conn.execute(
                "SELECT cost_est FROM jobs WHERE kind = 'grading' AND cost_est IS NOT NULL "
                "ORDER BY id DESC LIMIT 20"
            ).fetchall()
            average = (
                sum(row["cost_est"] for row in recent_costs) / len(recent_costs)
                if recent_costs else None
            )
            balance = latest["balance"] if latest else None
            forecast_checks = (
                int(balance / average) if balance is not None and average else None
            )
            recent_days = [
                day for day in expenses if 0 <= (today - day).days <= 6
            ]
            forecast_days = (
                balance / (expense_week / 7)
                if balance is not None and len(recent_days) >= 3 and expense_week > 0
                else None
            )
            cutoff = (utc_now() - timedelta(days=30)).isoformat().replace("+00:00", "Z")
            history = conn.execute(
                "SELECT * FROM balance_log WHERE at >= ? ORDER BY id", (cutoff,)
            ).fetchall()
            errors = conn.execute(
                "SELECT error, started_at FROM jobs WHERE error IS NOT NULL "
                "ORDER BY id DESC LIMIT 5"
            ).fetchall()
            billing = conn.execute(
                "SELECT value FROM settings WHERE key = 'billing_url'"
            ).fetchone()
            return {
                "state": state,
                "latest": latest,
                "reserve": estimate_reserve(self.settings),
                "queued": counts["queued"] or 0,
                "paused_jobs": counts["paused"] or 0,
                "expense_today": expense_today,
                "expense_week": expense_week,
                "expense_month": expense_month,
                "average_cost": average,
                "forecast_checks": forecast_checks,
                "forecast_days": forecast_days,
                "history": history,
                "history_points": _history_points(history),
                "errors": errors,
                "billing_url": billing["value"] if billing and billing["value"] else "https://proxyapi.ru",
            }
        finally:
            conn.close()


def _history_points(rows) -> str:
    if not rows:
        return ""
    values = [row["balance"] for row in rows if row["balance"] is not None]
    if not values:
        return ""
    low, high = min(values), max(values)
    span = high - low or 1.0
    points = []
    for index, value in enumerate(values):
        x = 0 if len(values) == 1 else index * 600 / (len(values) - 1)
        y = 165 - (value - low) / span * 145
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def _pause_jobs(conn: sqlite3.Connection) -> None:
    placeholders = ",".join("?" for _ in GPT_JOB_KINDS)
    conn.execute(
        f"UPDATE jobs SET status = 'paused_no_funds' "
        f"WHERE status = 'queued' AND kind IN ({placeholders})",
        GPT_JOB_KINDS,
    )


def _resume_jobs(conn: sqlite3.Connection) -> None:
    placeholders = ",".join("?" for _ in GPT_JOB_KINDS)
    conn.execute(
        f"UPDATE jobs SET status = 'queued' "
        f"WHERE status = 'paused_no_funds' AND kind IN ({placeholders})",
        GPT_JOB_KINDS,
    )
