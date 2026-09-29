"""Ответ бота в виде данных, не привязанных к Telegram: так его можно проверять в тестах."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Button:
    text: str
    data: str  # callback_data; в Telegram не длиннее 64 байт


def claim_button(post_id: int, text: str | None = None) -> Button:
    return Button(text or f"Беру №{post_id}", f"claim:{post_id}")


def stage_button(post_id: int, stage: int, text: str) -> Button:
    return Button(text, f"stage:{post_id}:{stage}")


@dataclass
class Reply:
    text: str | None = None  # HTML
    buttons: list[list[Button]] = field(default_factory=list)
    react: bool = False  # поставить 👍 на исходное сообщение
    post_ids: list[int] = field(default_factory=list)  # о каких постах сообщение: для «ответа на»
    kind: str = "reply"
    meta: dict[str, Any] = field(default_factory=dict)  # служебное: что отметить после отправки

    def is_empty(self) -> bool:
        return self.text is None and not self.react
