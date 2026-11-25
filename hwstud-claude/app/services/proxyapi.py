from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable

import httpx

from ..config import Settings


class Outcome(StrEnum):
    SUCCESS = "success"
    FATAL = "fatal"
    KEY_REJECTED = "key_rejected"
    NO_FUNDS = "no_funds"
    RATE_LIMITED = "rate_limited"
    RETRYABLE = "retryable"
    INVALID_RESPONSE = "invalid_response"


@dataclass(slots=True)
class ProxyResult:
    outcome: Outcome
    status_code: int | None = None
    data: dict[str, Any] | None = None
    content: dict[str, Any] | None = None
    error: str = ""
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    attempts: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome == Outcome.SUCCESS


class ProxyAPIClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.sleep = sleep

    async def chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_completion_tokens: int,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        timeout: float = 120,
    ) -> ProxyResult:
        if not self.settings.proxyapi_key:
            return ProxyResult(Outcome.FATAL, error="PROXYAPI_KEY не задан")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_completion_tokens": max_completion_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort

        attempts = 0
        rate_retries = 0
        network_retries = 0
        invalid_retries = 0
        network_delays = (30, 120, 600)
        async with httpx.AsyncClient(transport=self.transport) as client:
            while True:
                attempts += 1
                try:
                    response = await client.post(
                        f"{self.settings.proxyapi_base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.settings.proxyapi_key}"
                        },
                        json=payload,
                        timeout=timeout,
                    )
                except (httpx.NetworkError, httpx.TimeoutException) as exc:
                    if network_retries < len(network_delays):
                        delay = network_delays[network_retries]
                        network_retries += 1
                        await self.sleep(delay)
                        continue
                    return ProxyResult(
                        Outcome.RETRYABLE,
                        error=f"Сеть или тайм-аут: {type(exc).__name__}",
                        model=model,
                        attempts=attempts,
                    )

                status_code = response.status_code
                if status_code == 200:
                    try:
                        data = response.json()
                        content_text = data["choices"][0]["message"]["content"]
                        content = _decode_json_content(content_text)
                        if not isinstance(content, dict):
                            raise ValueError("JSON ответа не является объектом")
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        if invalid_retries < 1:
                            invalid_retries += 1
                            continue
                        return ProxyResult(
                            Outcome.INVALID_RESPONSE,
                            status_code=200,
                            error=f"Невалидный JSON: {type(exc).__name__}",
                            model=model,
                            attempts=attempts,
                        )
                    usage = data.get("usage") or {}
                    return ProxyResult(
                        Outcome.SUCCESS,
                        status_code=200,
                        data=data,
                        content=content,
                        model=data.get("model", model),
                        tokens_in=usage.get("prompt_tokens"),
                        tokens_out=usage.get("completion_tokens"),
                        attempts=attempts,
                        details={"usage": usage},
                    )

                error = _response_error(response)
                if status_code in {400, 404, 413, 422}:
                    outcome = Outcome.FATAL
                elif status_code in {401, 403}:
                    outcome = Outcome.KEY_REJECTED
                elif status_code == 402:
                    outcome = Outcome.NO_FUNDS
                elif status_code == 429:
                    if rate_retries < 5:
                        rate_retries += 1
                        await self.sleep(_retry_after(response, 60))
                        continue
                    outcome = Outcome.RATE_LIMITED
                elif status_code >= 500:
                    if network_retries < len(network_delays):
                        delay = network_delays[network_retries]
                        network_retries += 1
                        await self.sleep(delay)
                        continue
                    outcome = Outcome.RETRYABLE
                else:
                    outcome = Outcome.FATAL
                return ProxyResult(
                    outcome,
                    status_code=status_code,
                    error=error,
                    model=model,
                    attempts=attempts,
                )


def _decode_json_content(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        raise TypeError("Содержимое ответа не является строкой")
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().lower() in {"```json", "```"}:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    content = json.loads(text)
    if not isinstance(content, dict):
        raise ValueError("JSON ответа не является объектом")
    return content


def validate_grading_content(
    content: dict[str, Any], expected_reference_ids: set[int] | None = None
) -> str | None:
    if content.get("verdict") not in {"accept", "revise"}:
        return "verdict должен быть accept или revise"
    score = content.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 10:
        return "score должен быть целым числом от 0 до 10"
    confidence = content.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return "confidence должен быть числом"
    if not 0 <= confidence <= 1:
        return "confidence должен быть от 0 до 1"
    if not isinstance(content.get("errors"), list):
        return "errors должен быть списком"
    if not isinstance(content.get("comment_for_student"), str):
        return "comment_for_student должен быть строкой"
    matches = content.get("similarity_matches")
    if not isinstance(matches, list):
        return "similarity_matches должен быть списком"
    actual_reference_ids: set[int] = set()
    for match in matches:
        if not isinstance(match, dict):
            return "Каждое совпадение должно быть объектом"
        reference_id = match.get("reference_id")
        percent = match.get("match_percent")
        if isinstance(reference_id, bool) or not isinstance(reference_id, int) or reference_id < 1:
            return "reference_id должен быть положительным целым числом"
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            return "match_percent должен быть числом"
        if not 0 <= percent <= 100:
            return "match_percent должен быть от 0 до 100"
        if reference_id in actual_reference_ids:
            return "similarity_matches содержит повторный reference_id"
        actual_reference_ids.add(reference_id)
    if expected_reference_ids is not None and actual_reference_ids != expected_reference_ids:
        return "Модель должна вернуть ровно один процент для каждой работы сравнения"
    return None


def _response_error(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            error = data.get("error", data)
            if isinstance(error, dict):
                return str(error.get("message", error))[:1000]
            return str(error)[:1000]
    except ValueError:
        pass
    return (response.text or f"HTTP {response.status_code}")[:1000]


def _retry_after(response: httpx.Response, default: float) -> float:
    try:
        return max(0.0, float(response.headers.get("Retry-After", default)))
    except ValueError:
        return default
