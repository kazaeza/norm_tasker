"""Вызовы Telegram Bot API: HTTP через requests в отдельных потоках, типизированные методы."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
from collections.abc import Callable
from typing import Any, Protocol

from norm_tasker.tg.errors import NetworkError, error_for
from norm_tasker.tg.types import Chat, ChatMember, Message, User

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
CONNECT_TIMEOUT = 10
DEFAULT_TIMEOUT = 30
LINK_PREVIEW_OFF = {"is_disabled": True}
TOKEN_IN_URL = re.compile(r"/bot[^/\s]+/")


def scrub(text: str) -> str:
    """Токен бота входит в адрес запроса и не должен попасть в текст ошибки."""
    return TOKEN_IN_URL.sub("/bot<токен скрыт>/", text)


class Transport(Protocol):
    def post(self, url: str, payload: dict[str, Any], timeout: float) -> tuple[int, Any]:
        """Отправляет JSON и возвращает (HTTP-статус, разобранное тело или None)."""

    def close(self) -> None: ...


class HttpTransport:
    """Настоящая сеть. Блокирующая; вызывается из отдельного потока."""

    def __init__(self, retries: int = 2, backoff: float = 0.5) -> None:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        self._requests = requests
        self.session = requests.Session()
        # Повторяем только неудавшееся соединение: запрос при этом ещё не ушёл, дубля не будет.
        retry = Retry(
            total=retries, connect=retries, read=0, status=0, other=0, backoff_factor=backoff
        )
        self.session.mount("https://", HTTPAdapter(pool_maxsize=4, max_retries=retry))
        self.session.mount("http://", HTTPAdapter(pool_maxsize=4, max_retries=retry))

    def post(self, url: str, payload: dict[str, Any], timeout: float) -> tuple[int, Any]:
        try:
            response = self.session.post(url, json=payload, timeout=(CONNECT_TIMEOUT, timeout))
        except self._requests.RequestException as exc:
            raise NetworkError(scrub(f"{type(exc).__name__}: {exc}")) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body

    def close(self) -> None:
        self.session.close()


async def in_thread(func: Callable[..., Any], *args: Any) -> Any:
    """Выполняет блокирующий вызов в потоке-демоне и ждёт результат.

    Обычный asyncio.to_thread при остановке дождался бы конца долгого опроса (до ~30 с),
    а поток-демон просто обрывается вместе с процессом.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def finish(setter: Callable[[Any], None], value: Any) -> None:
        if not future.done():  # ожидание отменили — результат никому не нужен
            setter(value)

    def work() -> None:
        try:
            outcome: tuple[Callable[[Any], None], Any] = (future.set_result, func(*args))
        except BaseException as exc:
            outcome = (future.set_exception, exc)
        with contextlib.suppress(RuntimeError):  # цикл событий уже закрыт
            loop.call_soon_threadsafe(finish, *outcome)

    threading.Thread(target=work, daemon=True, name="telegram-http").start()
    return await future


class Api:
    def __init__(
        self, token: str, *, transport: Transport | None = None, base_url: str = API_URL
    ) -> None:
        self.token = token
        self.transport: Transport = transport or HttpTransport()
        self.base_url = base_url.rstrip("/")

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT
    ) -> Any:
        """Один вызов метода Bot API. Параметры со значением None не отправляются."""
        payload = {key: value for key, value in (params or {}).items() if value is not None}
        url = f"{self.base_url}/bot{self.token}/{method}"
        try:
            status, body = await in_thread(self.transport.post, url, payload, timeout)
        except NetworkError as error:
            raise NetworkError(error.description, method=method) from None
        if isinstance(body, dict) and body.get("ok") is True:
            return body.get("result")
        raise error_for(method, status, body)

    async def close(self) -> None:
        self.transport.close()

    # --- методы, которыми пользуется бот -----------------------------------------------------
    async def get_me(self) -> User:
        user = User.from_dict(await self.call("getMe"))
        assert user is not None
        return user

    async def get_updates(
        self, offset: int | None, timeout: int, allowed_updates: list[str]
    ) -> list[dict[str, Any]]:
        result = await self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": allowed_updates},
            timeout=timeout + 15,
        )
        return list(result or [])

    async def delete_webhook(self) -> None:
        """Без этого getUpdates не работает, если когда-то был включён вебхук."""
        await self.call("deleteWebhook", {"drop_pending_updates": False})

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = "HTML",
        reply_markup: dict[str, Any] | None = None,
        reply_to: int | None = None,
        thread_id: int | None = None,
    ) -> Message:
        result = await self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
                "reply_parameters": (
                    {"message_id": reply_to, "allow_sending_without_reply": True}
                    if reply_to
                    else None
                ),
                "message_thread_id": thread_id,
                "link_preview_options": LINK_PREVIEW_OFF,
            },
        )
        message = Message.from_dict(result)
        assert message is not None
        return message

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = "HTML",
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        await self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,  # без разметки кнопки исчезают
                "link_preview_options": LINK_PREVIEW_OFF,
            },
        )

    async def remove_buttons(self, chat_id: int, message_id: int) -> None:
        await self.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id})

    async def set_message_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        await self.call(
            "setMessageReaction",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": emoji}],
            },
        )

    async def send_chat_action(
        self, chat_id: int, action: str = "typing", thread_id: int | None = None
    ) -> None:
        """«Печатает…» в чате: показывает, что бот думает над ответом."""
        await self.call(
            "sendChatAction",
            {"chat_id": chat_id, "action": action, "message_thread_id": thread_id},
            timeout=10,
        )

    async def pin_chat_message(self, chat_id: int, message_id: int, *, silent: bool = True) -> None:
        await self.call(
            "pinChatMessage",
            {"chat_id": chat_id, "message_id": message_id, "disable_notification": silent},
        )

    async def answer_callback_query(
        self, query_id: str, text: str | None = None, *, show_alert: bool = False
    ) -> None:
        await self.call(
            "answerCallbackQuery",
            {"callback_query_id": query_id, "text": text, "show_alert": show_alert},
        )

    async def get_chat(self, chat_id: int) -> Chat:
        chat = Chat.from_dict(await self.call("getChat", {"chat_id": chat_id}))
        assert chat is not None
        return chat

    async def get_chat_member(self, chat_id: int, user_id: int) -> ChatMember:
        member = ChatMember.from_dict(
            await self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        )
        assert member is not None
        return member
