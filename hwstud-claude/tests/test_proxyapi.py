from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.services.proxyapi import (
    Outcome,
    ProxyAPIClient,
    validate_grading_content,
)


def response_for(status: int, content=None) -> httpx.Response:
    if status == 200:
        body = {
            "model": "test-model",
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        return httpx.Response(200, json=body)
    return httpx.Response(status, json={"error": {"message": f"HTTP {status}"}})


async def run_chat(settings, handler):
    client = ProxyAPIClient(
        settings,
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    return await client.chat_completion(
        model="test-model",
        messages=[{"role": "user", "content": "test"}],
        max_completion_tokens=100,
        response_format={"type": "json_object"},
    )


async def _no_sleep(_delay: float) -> None:
    return None


def valid_content():
    return {
        "verdict": "accept",
        "score": 8,
        "errors": [],
        "classification": "good",
        "confidence": 0.9,
        "comment_for_student": "Принято",
    }


def test_success_and_usage(settings):
    result = asyncio.run(run_chat(settings, lambda _request: response_for(200, valid_content())))
    assert result.ok
    assert result.content["score"] == 8
    assert result.tokens_in == 100
    assert result.tokens_out == 20
    assert result.attempts == 1


@pytest.mark.parametrize(
    ("status_code", "outcome"),
    [
        (400, Outcome.FATAL),
        (404, Outcome.FATAL),
        (413, Outcome.FATAL),
        (422, Outcome.FATAL),
        (401, Outcome.KEY_REJECTED),
        (403, Outcome.KEY_REJECTED),
        (402, Outcome.NO_FUNDS),
    ],
)
def test_non_retryable_response_table(settings, status_code, outcome):
    result = asyncio.run(run_chat(settings, lambda _request: response_for(status_code)))
    assert result.outcome == outcome
    assert result.status_code == status_code
    assert result.attempts == 1


def test_rate_limit_uses_stubbed_sleep_and_retries(settings):
    responses = iter((response_for(429), response_for(200, valid_content())))
    delays = []

    async def sleep(delay):
        delays.append(delay)

    async def run():
        client = ProxyAPIClient(
            settings,
            transport=httpx.MockTransport(lambda _request: next(responses)),
            sleep=sleep,
        )
        return await client.chat_completion(
            model="test-model", messages=[], max_completion_tokens=100
        )

    result = asyncio.run(run())
    assert result.ok
    assert result.attempts == 2
    assert delays == [60]


def test_server_error_stops_after_three_retries(settings):
    result = asyncio.run(run_chat(settings, lambda _request: response_for(500)))
    assert result.outcome == Outcome.RETRYABLE
    assert result.attempts == 4


def test_invalid_json_retries_once(settings):
    result = asyncio.run(
        run_chat(
            settings,
            lambda _request: httpx.Response(
                200, json={"choices": [{"message": {"content": "not-json"}}]}
            ),
        )
    )
    assert result.outcome == Outcome.INVALID_RESPONSE
    assert result.attempts == 2


def test_markdown_fenced_json_is_accepted(settings):
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "```json\n" + json.dumps(valid_content()) + "\n```"}}],
                "usage": {},
            },
        )

    result = asyncio.run(run_chat(settings, handler))
    assert result.ok
    assert result.content["score"] == 8


def test_grading_schema_validation():
    assert validate_grading_content(valid_content()) is None
    for score in (-1, 11, 2.5, True):
        content = valid_content()
        content["score"] = score
        assert validate_grading_content(content)
