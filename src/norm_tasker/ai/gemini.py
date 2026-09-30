"""Клиент Gemini API: вопрос текстом — ответ текстом.

Ключ уходит заголовком, а не в адресе, чтобы не попасть в журнал и в тексты ошибок. Имена моделей
у Google меняются, поэтому по умолчанию бот перебирает список и запоминает первую рабочую.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Protocol

from norm_tasker.tg.api import in_thread

log = logging.getLogger(__name__)

DEFAULT_URL = "https://generativelanguage.googleapis.com"
# Сначала самые лёгкие и быстрые. «-latest» — псевдонимы Google, которые сами переходят на новую
# версию; если их у ключа нет, пробуем конкретные имена.
DEFAULT_MODELS = (
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
    "gemini-3.8-flash",
)
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 40
GOOGLE_KEY = re.compile(r"AIza[\w-]{20,}")


class AiError(Exception):
    """ИИ не ответил. Текст понятен человеку и не содержит ключа."""


class AiTransport(Protocol):
    def post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float
    ) -> tuple[int, Any]:
        """Отправляет JSON и возвращает (HTTP-статус, разобранное тело или None)."""


class HttpAiTransport:
    """Настоящая сеть. Блокирующая; вызывается из отдельного потока."""

    def post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float
    ) -> tuple[int, Any]:
        import requests

        try:
            response = requests.post(
                url, headers=headers, json=payload, timeout=(CONNECT_TIMEOUT, timeout)
            )
        except requests.RequestException as exc:
            raise AiError(f"нет связи с Gemini ({type(exc).__name__})") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body


def _clean(text: str, limit: int = 200) -> str:
    return GOOGLE_KEY.sub("<ключ скрыт>", " ".join(text.split()))[:limit]


def _api_message(body: Any) -> str:
    error = body.get("error") if isinstance(body, dict) else None
    return _clean(str(error.get("message") or "")) if isinstance(error, dict) else ""


def describe_failure(status: int, body: Any) -> str:
    """Что случилось, словами: по ответу Gemini на неудачный запрос."""
    message = _api_message(body)
    low = message.lower()
    if "api key" in low and status in (400, 401, 403):
        return "Gemini не принял ключ: проверьте GEMINI_API"
    if status == 403 and "location" in low:
        return "Gemini недоступен из региона, где стоит сервер"
    if status in (401, 403):
        return f"Gemini не пустил (HTTP {status}): {message or 'нет доступа'}"
    if status == 404:
        return f"Gemini: не найдено ({message or 'нет такой модели или адреса'})"
    if status == 429:
        return "Gemini: исчерпан лимит запросов (429)"
    if status >= 500:
        return f"Gemini временно недоступен (HTTP {status})"
    return f"Gemini ответил ошибкой (HTTP {status}): {message or 'без пояснения'}"


def extract_text(body: Any) -> str:
    """Текст первого варианта ответа. Пустой ответ или отказ фильтра — ошибка."""
    if not isinstance(body, dict):
        raise AiError("Gemini прислал ответ, который не удалось прочитать")
    candidates = body.get("candidates") or []
    if not candidates:
        blocked = (body.get("promptFeedback") or {}).get("blockReason")
        raise AiError(
            f"Gemini отказался отвечать (фильтр: {blocked})" if blocked else "Gemini не дал ответа"
        )
    first = candidates[0] if isinstance(candidates[0], dict) else {}
    parts = (first.get("content") or {}).get("parts") or []
    text = "".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and not part.get("thought")
    ).strip()
    if not text:
        reason = first.get("finishReason") or "без причины"
        raise AiError(f"Gemini вернул пустой текст ({reason})")
    return text


class GeminiClient:
    def __init__(
        self,
        key: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
        transport: AiTransport | None = None,
    ) -> None:
        self._key = key
        self._models = (model,) if model else DEFAULT_MODELS
        self.model: str | None = model  # рабочая модель; пока неизвестна — None
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        self.transport: AiTransport = transport or HttpAiTransport()

    async def ask(
        self, system: str, prompt: str, *, max_tokens: int = 4096, temperature: float | None = None
    ) -> str:
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if (
            temperature is not None
        ):  # у новых моделей Google советует оставлять значение по умолчанию
            payload["generationConfig"]["temperature"] = temperature
        headers = {"x-goog-api-key": self._key, "Content-Type": "application/json"}
        models = [self.model] if self.model else list(self._models)
        failure = AiError("Gemini: не нашлось подходящей модели")
        for model in models:
            url = f"{self.base_url}/v1beta/models/{model}:generateContent"
            started = time.monotonic()
            status, body = await in_thread(self.transport.post, url, headers, payload, READ_TIMEOUT)
            if status == 404:  # такой модели у этого ключа нет: пробуем следующую
                failure = AiError(describe_failure(status, body) + f" [{model}]")
                continue
            if status != 200:
                raise AiError(describe_failure(status, body))
            text = extract_text(body)
            self.model = model
            log.info("Gemini (%s) ответил за %.1f с", model, time.monotonic() - started)
            return text
        raise failure

    async def ping(self) -> str:
        """Самый короткий запрос: проверка ключа, адреса и модели."""
        return await self.ask("Отвечай одним словом.", "Ответь одним словом: ок", max_tokens=1024)
