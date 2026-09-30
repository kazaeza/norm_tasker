"""Последние сообщения рабочего чата: контекст для ИИ, чтобы «готово» не повисало в воздухе.

Хранится только в памяти и только последние полчаса: после перезапуска бота журнал пуст.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

LINE_LIMIT = 300


@dataclass(frozen=True)
class ChatLine:
    at: datetime
    author: str
    text: str


class ChatLog:
    def __init__(self, keep: int = 30, max_age: timedelta = timedelta(minutes=30)) -> None:
        self._lines: deque[ChatLine] = deque(maxlen=keep)
        self.max_age = max_age

    def add(self, at: datetime, author: str, text: str) -> None:
        text = " ".join(text.split())[:LINE_LIMIT]
        if text:
            self._lines.append(ChatLine(at, author, text))

    def recent(self, now: datetime, limit: int = 6) -> list[ChatLine]:
        """Последние сообщения за полчаса, старые первыми."""
        fresh = [line for line in self._lines if now - line.at <= self.max_age]
        return fresh[-limit:]
