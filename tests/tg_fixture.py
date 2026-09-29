"""Подставной Telegram: апдейты идут через настоящий диспетчер aiogram, а вместо сети —
запись вызовов API и заготовленные ответы."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    Message,
    Update,
    User,
)

BOT_ID = 42
WORK_CHAT = -1001234567890


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 1000
        self.reads_all = True
        self.can_pin = True
        self.reject_reactions = False
        self.reject_pin = False

    async def close(self) -> None:
        return None

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        name = type(method).__name__
        self.calls.append((name, method.model_dump(exclude_none=True)))
        handler = getattr(self, f"_{name}", None)
        return handler(method) if handler else True

    # --- ответы на вызовы ---------------------------------------------------------------
    def _GetMe(self, method: Any) -> User:
        return User(
            id=BOT_ID, is_bot=True, first_name="Тест", username="norm_test_bot",
            can_read_all_group_messages=self.reads_all,
        )  # fmt: skip

    def _SendMessage(self, method: Any) -> Message:
        self._next_id += 1
        return Message(
            message_id=self._next_id,
            date=datetime.now(),
            chat=Chat(id=method.chat_id, type="supergroup"),
            text=method.text,
            from_user=User(id=BOT_ID, is_bot=True, first_name="Тест"),
        )

    def _GetChatMember(self, method: Any) -> ChatMemberAdministrator:
        return ChatMemberAdministrator.model_construct(
            status="administrator",
            user=User(id=BOT_ID, is_bot=True, first_name="Тест"),
            can_pin_messages=self.can_pin,
        )

    def _SetMessageReaction(self, method: Any) -> bool:
        if self.reject_reactions:
            from aiogram.exceptions import TelegramBadRequest

            raise TelegramBadRequest(method, "REACTION_INVALID")
        return True

    def _PinChatMessage(self, method: Any) -> bool:
        if self.reject_pin:
            from aiogram.exceptions import TelegramBadRequest

            raise TelegramBadRequest(method, "not enough rights to pin a message")
        return True

    # --- удобные выборки ---------------------------------------------------------------------
    def named(self, name: str) -> list[dict[str, Any]]:
        return [data for call, data in self.calls if call == name]

    def sent_texts(self) -> list[str]:
        return [data["text"] for data in self.named("SendMessage")]

    def last_text(self) -> str:
        return self.sent_texts()[-1]

    def clear(self) -> None:
        self.calls.clear()


def make_user(user_id: int, username: str | None, name: str) -> User:
    return User(id=user_id, is_bot=False, first_name=name, username=username)


_counter = {"update": 0, "message": 5000}


def _next(kind: str) -> int:
    _counter[kind] += 1
    return _counter[kind]


def text_update(
    text: str,
    *,
    user_id: int,
    username: str | None,
    name: str = "Участник",
    chat_id: int = WORK_CHAT,
    reply_to: Message | None = None,
    chat_type: str = "supergroup",
) -> Update:
    message = Message(
        message_id=_next("message"),
        date=datetime.now(),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=make_user(user_id, username, name),
        text=text,
        reply_to_message=reply_to,
    )
    return Update(update_id=_next("update"), message=message)


def bot_message(message_id: int, chat_id: int = WORK_CHAT) -> Message:
    """Сообщение, которое бот отправил раньше: на него можно ответить."""
    return Message(
        message_id=message_id,
        date=datetime.now(),
        chat=Chat(id=chat_id, type="supergroup"),
        from_user=User(id=BOT_ID, is_bot=True, first_name="Тест"),
        text="сообщение бота",
    )


def callback_update(
    data: str,
    *,
    user_id: int,
    username: str | None,
    message_id: int,
    name: str = "Участник",
    chat_id: int = WORK_CHAT,
) -> Update:
    query = CallbackQuery(
        id=str(_next("update")),
        from_user=make_user(user_id, username, name),
        chat_instance="test",
        message=bot_message(message_id, chat_id),
        data=data,
    )
    return Update(update_id=_next("update"), callback_query=query)


class FakeGoogle:
    """Google без сети: отдаёт заготовленную выгрузку КП и комментарии доков."""

    service_account_email = "bot@example.iam.gserviceaccount.com"

    def __init__(self, xlsx: bytes = b"", doc_comments: dict[str, list] | None = None) -> None:
        self.xlsx = xlsx
        self.docs = doc_comments or {}
        self.written: list[list[list[str]]] = []
        self.tabs: dict[str, int] = {}

    def export_xlsx(self, file_id: str) -> bytes:
        return self.xlsx

    def doc_comments(self, file_id: str) -> list:
        return list(self.docs.get(file_id, []))

    def sheet_tabs(self, spreadsheet_id: str) -> dict[str, int]:
        return dict(self.tabs)

    def add_tab(self, spreadsheet_id: str, title: str) -> None:
        self.tabs[title] = len(self.tabs)

    def replace_values(self, spreadsheet_id: str, tab: str, rows: list[list[str]]) -> None:
        self.written.append(rows)
