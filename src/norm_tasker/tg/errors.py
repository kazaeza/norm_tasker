"""Ошибки вызова Telegram Bot API."""

from __future__ import annotations

from typing import Any


class TelegramError(Exception):
    """Любой сбой вызова: ответ Telegram с ok=false или проблема с сетью."""

    def __init__(
        self,
        description: str,
        *,
        method: str = "",
        code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(f"{method}: {description}" if method else description)
        self.description = description
        self.method = method
        self.code = code
        self.retry_after = retry_after


class BadRequest(TelegramError):
    """400: запрос неверный (нет прав, сообщение не найдено, разметка не разобрана)."""


class Unauthorized(TelegramError):
    """401/404: токен бота не принят."""


class Forbidden(TelegramError):
    """403: бота нет в чате, его выгнали или заблокировали."""


class Conflict(TelegramError):
    """409: обновления уже читает другой экземпляр бота (или включён вебхук)."""


class RetryAfter(TelegramError):
    """429: слишком много запросов, надо подождать retry_after секунд."""


class NetworkError(TelegramError):
    """Нет связи, таймаут, ошибка на стороне Telegram (5xx) или ответ не похож на JSON."""


def error_for(method: str, status: int, body: Any) -> TelegramError:
    """Собирает ошибку из HTTP-статуса и тела ответа Telegram."""
    data = body if isinstance(body, dict) else {}
    code = data.get("error_code") or status
    description = str(data.get("description") or f"HTTP {status}")
    params = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
    retry_after = params.get("retry_after")
    fields = {"method": method, "code": code}
    if code in (420, 429):
        return RetryAfter(description, retry_after=int(retry_after or 1), **fields)
    if not data or code >= 500:
        return NetworkError(description, **fields)
    if code in (401, 404):
        return Unauthorized(description, **fields)
    if code == 400:
        return BadRequest(description, **fields)
    if code == 403:
        return Forbidden(description, **fields)
    if code == 409:
        return Conflict(description, **fields)
    return TelegramError(description, **fields)
