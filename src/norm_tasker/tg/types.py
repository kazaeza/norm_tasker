"""Типы Telegram Bot API, которые нужны боту.

Разбор JSON руками и только нужные поля: так они занимают килобайты, а не сотни мегабайт.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

Json = dict[str, Any]


@dataclass(slots=True)
class User:
    id: int
    is_bot: bool = False
    first_name: str = ""
    last_name: str | None = None
    username: str | None = None
    can_read_all_group_messages: bool | None = None  # приходит только из getMe

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}" if self.last_name else self.first_name

    @classmethod
    def from_dict(cls, data: Json | None) -> User | None:
        if not data:
            return None
        return cls(
            id=data["id"],
            is_bot=bool(data.get("is_bot")),
            first_name=data.get("first_name") or "",
            last_name=data.get("last_name"),
            username=data.get("username"),
            can_read_all_group_messages=data.get("can_read_all_group_messages"),
        )


@dataclass(slots=True)
class Chat:
    id: int
    type: str = "private"  # private, group, supergroup, channel
    title: str | None = None
    is_forum: bool = False

    @classmethod
    def from_dict(cls, data: Json | None) -> Chat | None:
        if not data:
            return None
        return cls(
            id=data["id"],
            type=data.get("type") or "private",
            title=data.get("title"),
            is_forum=bool(data.get("is_forum")),
        )


@dataclass(slots=True)
class ChatMember:
    status: str  # creator, administrator, member, restricted, left, kicked
    user: User | None = None
    can_pin_messages: bool | None = None

    @classmethod
    def from_dict(cls, data: Json | None) -> ChatMember | None:
        if not data:
            return None
        return cls(
            status=data.get("status") or "",
            user=User.from_dict(data.get("user")),
            can_pin_messages=data.get("can_pin_messages"),
        )


@dataclass(slots=True)
class Message:
    message_id: int
    chat: Chat
    from_user: User | None = None
    text: str | None = None
    caption: str | None = None
    reply_to_message: Message | None = None
    message_thread_id: int | None = None
    is_topic_message: bool = False

    @classmethod
    def from_dict(cls, data: Json | None) -> Message | None:
        if not data:
            return None
        chat = Chat.from_dict(data["chat"])
        assert chat is not None
        return cls(
            message_id=data["message_id"],
            chat=chat,
            from_user=User.from_dict(data.get("from")),
            text=data.get("text"),
            caption=data.get("caption"),
            reply_to_message=cls.from_dict(data.get("reply_to_message")),
            message_thread_id=data.get("message_thread_id"),
            is_topic_message=bool(data.get("is_topic_message")),
        )


@dataclass(slots=True)
class CallbackQuery:
    id: str
    from_user: User
    message: Message | None = None  # у старых сообщений приходит урезанным: только чат и номер
    data: str | None = None

    @classmethod
    def from_dict(cls, data: Json | None) -> CallbackQuery | None:
        if not data:
            return None
        user = User.from_dict(data["from"])
        assert user is not None
        return cls(
            id=str(data["id"]),
            from_user=user,
            message=Message.from_dict(data.get("message")),
            data=data.get("data"),
        )


@dataclass(slots=True)
class ChatMemberUpdated:
    chat: Chat
    old_chat_member: ChatMember
    new_chat_member: ChatMember
    from_user: User | None = None

    @classmethod
    def from_dict(cls, data: Json | None) -> ChatMemberUpdated | None:
        if not data:
            return None
        chat = Chat.from_dict(data["chat"])
        old = ChatMember.from_dict(data["old_chat_member"])
        new = ChatMember.from_dict(data["new_chat_member"])
        assert chat is not None and old is not None and new is not None
        return cls(
            chat=chat,
            old_chat_member=old,
            new_chat_member=new,
            from_user=User.from_dict(data.get("from")),
        )


@dataclass(slots=True)
class Update:
    update_id: int
    message: Message | None = None
    callback_query: CallbackQuery | None = None
    my_chat_member: ChatMemberUpdated | None = None

    @classmethod
    def from_dict(cls, data: Json) -> Update:
        return cls(
            update_id=data["update_id"],
            message=Message.from_dict(data.get("message")),
            callback_query=CallbackQuery.from_dict(data.get("callback_query")),
            my_chat_member=ChatMemberUpdated.from_dict(data.get("my_chat_member")),
        )
