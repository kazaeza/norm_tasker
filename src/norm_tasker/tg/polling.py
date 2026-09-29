"""Long polling: забираем обновления у Telegram и раздаём обработчику."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from norm_tasker.tg.api import Api
from norm_tasker.tg.errors import Conflict, RetryAfter, TelegramError, Unauthorized
from norm_tasker.tg.types import Update

log = logging.getLogger(__name__)

POLL_TIMEOUT = 25  # секунд ждёт один запрос getUpdates
CONFLICT_PAUSE = 10
MAX_BACKOFF = 30


async def poll_updates(
    api: Api,
    handle: Callable[[Update], Awaitable[None]],
    *,
    allowed_updates: list[str],
    offset: int | None = None,
    save_offset: Callable[[int], None] | None = None,
    timeout: int = POLL_TIMEOUT,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Работает, пока задачу не отменят. Неверный токен — единственная ошибка, что её прерывает.

    offset — с какого обновления читать; save_offset вызывается после каждого разобранного
    обновления, чтобы после перезапуска не разбирать его второй раз.
    """
    try:
        await api.delete_webhook()
    except Unauthorized:
        raise
    except TelegramError as error:
        log.warning("Не удалось сбросить вебхук: %s", error)

    backoff = 1.0
    while True:
        try:
            raw_updates = await api.get_updates(offset, timeout, allowed_updates)
        except Unauthorized:
            raise
        except Conflict as error:
            log.warning(
                "Telegram: %s. Возможно, бот запущен ещё где-то — жду %s с", error, CONFLICT_PAUSE
            )
            delay = float(CONFLICT_PAUSE)
        except RetryAfter as error:
            log.warning("Telegram просит подождать %s с", error.retry_after)
            delay = (error.retry_after or 1) + 1.0
        except TelegramError as error:
            log.warning("Опрос Telegram не удался: %s. Повтор через %.0f с", error, backoff)
            delay = backoff
            backoff = min(backoff * 2, MAX_BACKOFF)
        else:
            backoff = 1.0
            for raw in raw_updates:
                offset = await _handle_one(raw, handle, save_offset, offset)
            continue
        await sleep(delay)


async def _handle_one(
    raw: dict,
    handle: Callable[[Update], Awaitable[None]],
    save_offset: Callable[[int], None] | None,
    offset: int | None,
) -> int | None:
    update_id = raw.get("update_id")
    try:
        await handle(Update.from_dict(raw))
    except asyncio.CancelledError:
        raise  # обновление не разобрано до конца: после перезапуска придёт снова
    except Exception:
        log.exception("Не удалось обработать обновление %s", update_id)
    if not isinstance(update_id, int):
        return offset
    if save_offset is not None:
        try:
            save_offset(update_id + 1)
        except Exception:
            log.exception("Не удалось сохранить позицию чтения обновлений")
    return update_id + 1
