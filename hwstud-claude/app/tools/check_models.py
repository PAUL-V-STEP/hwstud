from __future__ import annotations

import argparse
import base64
import io
import json
from datetime import UTC, datetime
from typing import Any

from PIL import Image, ImageDraw

from ..config import load_settings
from ..db import connect, transaction
from ..services.proxyapi import Outcome, ProxyAPIClient

PROMPT = (
    "Проверка технической совместимости. Верни JSON-объект "
    '{"answer":"ok","received":true}. Не добавляй пояснений.'
)
RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "model_check",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "received": {"type": "boolean"},
            },
            "required": ["answer", "received"],
            "additionalProperties": False,
        },
    },
}


def test_image() -> str:
    image = Image.new("RGB", (1600, 1600), "#f6f4ee")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 1500, 1500), outline="#2b59c3", width=12)
    for position in range(200, 1500, 200):
        draw.line((position, 100, position, 1500), fill="#e4e0d6", width=2)
        draw.line((100, position, 1500, position), fill="#e4e0d6", width=2)
    draw.rectangle((500, 500, 1100, 1100), fill="#12a39a")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


async def _request(
    client: ProxyAPIClient,
    model: str,
    content: Any,
    response_format: dict[str, Any],
):
    return await client.chat_completion(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_completion_tokens=100,
        response_format=response_format,
    )


async def check_model(client: ProxyAPIClient, model: str, image_url: str) -> dict[str, Any]:
    selected_format = RESPONSE_SCHEMA
    text_result = await _request(client, model, PROMPT, selected_format)
    fallback = False
    if (
        text_result.outcome == Outcome.FATAL
        and text_result.status_code in {400, 404, 422}
    ):
        selected_format = {"type": "json_object"}
        fallback = True
        text_result = await _request(client, model, PROMPT, selected_format)
    if not text_result.ok or text_result.content.get("answer") != "ok":
        raise RuntimeError(f"{model}: текстовый JSON не прошёл: {text_result.error}")

    image_content = [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]
    image_result = await _request(client, model, image_content, selected_format)
    if not image_result.ok or image_result.content.get("answer") != "ok":
        raise RuntimeError(f"{model}: запрос с картинкой не прошёл: {image_result.error}")

    usage = image_result.details.get("usage", {})
    details = usage.get("prompt_tokens_details") or {}
    image_tokens = details.get("image_tokens")
    method = "usage.prompt_tokens_details.image_tokens"
    if image_tokens is None:
        if text_result.tokens_in is None or image_result.tokens_in is None:
            image_tokens = 1500
            method = "оценка по умолчанию"
        else:
            image_tokens = image_result.tokens_in - text_result.tokens_in
            method = "разность prompt_tokens"
    return {
        "model": model,
        "response_format": "json_object" if fallback else "json_schema",
        "fallback_used": fallback,
        "text_prompt_tokens": text_result.tokens_in,
        "image_prompt_tokens": image_result.tokens_in,
        "image_tokens": image_tokens,
        "image_tokens_method": method,
        "image_size": "1600x1600 PNG",
        "text_json_ok": True,
        "image_json_ok": True,
    }


def append_report(results: list[dict[str, Any]]) -> None:
    root = load_settings().base_dir
    target = root / "TESTING.md"
    existing = target.read_text(encoding="utf-8") if target.exists() else "# Тестирование\n"
    lines = [
        "",
        f"## Проверка моделей — {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        "Выполнены отдельные проверочные запросы после `--confirm-paid`.",
        "",
        "| Модель | JSON | Картинка | image_tokens | Способ |",
        "|---|---|---|---:|---|",
    ]
    for result in results:
        lines.append(
            f"| `{result['model']}` | {result['response_format']} | OK | "
            f"{result['image_tokens']} | {result['image_tokens_method']} |"
        )
    lines.extend(["", "Тестовая картинка: 1600 × 1600 px, PNG.", ""])
    target.write_text(existing.rstrip() + "\n" + "\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Платная проверка моделей ProxyAPI: JSON и изображение"
    )
    parser.add_argument(
        "--confirm-paid",
        action="store_true",
        help="Подтвердить отдельные запросы, которые могут быть платными",
    )
    args = parser.parse_args()
    if not args.confirm_paid:
        raise SystemExit(
            "Запросы не выполнены. Для реальной проверки моделей добавьте --confirm-paid. "
            "Они могут быть платными."
        )

    import asyncio

    async def run() -> list[dict[str, Any]]:
        settings = load_settings()
        if not settings.proxyapi_key:
            raise SystemExit("Сначала задайте PROXYAPI_KEY в .env")
        client = ProxyAPIClient(settings)
        image_url = test_image()
        results = []
        for model in dict.fromkeys((settings.model_grading, settings.model_aux)):
            result = await check_model(client, model, image_url)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False))
        return results

    results = asyncio.run(run())
    settings = load_settings()
    grading_result = next(
        result for result in results if result["model"] == settings.model_grading
    )
    conn = connect(settings)
    try:
        with transaction(conn):
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'image_tokens'",
                (str(grading_result["image_tokens"]),),
            )
            conn.execute("UPDATE settings SET value = '1' WHERE key = 'models_confirmed'")
    finally:
        conn.close()
    append_report(results)
    print("Результаты добавлены в TESTING.md")


if __name__ == "__main__":
    main()
