"""Отправка сообщений в Telegram: кнопки, ответы, реакции, повторы при ограничениях."""

from __future__ import annotations

import asyncio
import logging
import re
from html import unescape

from norm_tasker.config import Settings
from norm_tasker.reply import Button, Reply
from norm_tasker.tg.api import Api
from norm_tasker.tg.errors import BadRequest, Forbidden, RetryAfter, TelegramError
from norm_tasker.tg.types import Message
from norm_tasker.tracker.service import Tracker

log = logging.getLogger(__name__)

TAGS = re.compile(r"<[^>]+>")
MAX_LENGTH = 4096
ATTEMPTS = 3


def keyboard(rows: list[list[Button]]) -> dict | None:
    rows = [row for row in rows if row]
    if not rows:
        return None
    return {
        "inline_keyboard": [
            [{"text": b.text, "callback_data": b.data} for b in row] for row in rows
        ]
    }


def message_link(chat_id: int, message_id: int) -> str | None:
    """Ссылка на сообщение в супергруппе; для обычных групп ссылок нет."""
    text = str(chat_id)
    return f"https://t.me/c/{text[4:]}/{message_id}" if text.startswith("-100") else None


def clip_message(text: str) -> str:
    if len(text) <= MAX_LENGTH:
        return text
    return text[: MAX_LENGTH - 2].rsplit("\n", 1)[0] + "\n…"


class Sender:
    def __init__(self, bot: Api, tracker: Tracker, settings: Settings) -> None:
        self.bot = bot
        self.tracker = tracker
        self.settings = settings

    @property
    def chat_id(self) -> int | None:
        return self.settings.chat_id

    async def send(
        self,
        reply: Reply,
        *,
        reply_to: int | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> Message | None:
        """Отправляет ответ в рабочий чат. None — отправить нечего или не вышло."""
        chat = chat_id if chat_id is not None else self.chat_id
        if reply.text is None or chat is None:
            return None
        thread = thread_id if thread_id is not None else self.settings.thread_id
        text = clip_message(reply.text)
        markup = keyboard(reply.buttons)
        plain = False
        for attempt in range(ATTEMPTS):
            try:
                message = await self.bot.send_message(
                    chat,
                    unescape(TAGS.sub("", text)) if plain else text,
                    parse_mode=None if plain else "HTML",
                    reply_markup=markup,
                    reply_to=reply_to,
                    thread_id=thread,
                )
            except RetryAfter as error:
                log.warning("Telegram просит подождать %s с", error.retry_after)
                await asyncio.sleep((error.retry_after or 1) + 1)
                continue
            except Forbidden:
                log.error("Бота нет в чате %s или у него нет прав писать", chat)
                return None
            except BadRequest as error:
                text_error = str(error).lower()
                if "can't parse entities" in text_error and not plain:
                    log.warning("Разметка не принята, отправляю без неё: %s", error)
                    plain = True
                    continue
                if "thread not found" in text_error and thread is not None:
                    thread = None
                    continue
                log.error("Не удалось отправить сообщение: %s", error)
                return None
            except (TelegramError, OSError):
                log.exception("Сбой при отправке сообщения (попытка %s)", attempt + 1)
                await asyncio.sleep(2**attempt)
                continue
            if reply.post_ids or reply.kind in ("summary", "free", "ask", "confirm", "remind"):
                self.tracker.state.remember_message(
                    chat, message.message_id, reply.kind, reply.post_ids, self.tracker.now()
                )
            return message
        return None

    async def react(self, chat_id: int, message_id: int, emoji: str = "👍") -> bool:
        try:
            await self.bot.set_message_reaction(chat_id, message_id, emoji)
        except TelegramError as error:
            log.info("Не удалось поставить реакцию: %s", error)
            return False
        return True

    async def edit(
        self, chat_id: int, message_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> bool:
        try:
            await self.bot.edit_message_text(
                chat_id,
                message_id,
                clip_message(text),
                reply_markup=keyboard(buttons or []),
            )
        except BadRequest as error:
            if "message is not modified" in str(error).lower():
                return True
            log.info("Не удалось изменить сообщение: %s", error)
            return False
        except TelegramError as error:
            log.info("Не удалось изменить сообщение: %s", error)
            return False
        return True

    async def remove_buttons(self, chat_id: int, message_id: int) -> None:
        try:
            await self.bot.remove_buttons(chat_id, message_id)
        except TelegramError as error:
            log.info("Не удалось убрать кнопки: %s", error)
