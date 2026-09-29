"""Данные, которые парсер достаёт из контент-плана."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

DOC_MARKER = re.compile(r"^\s*\[\s*док\s*\]\s*", re.IGNORECASE)
DOC_URL = re.compile(r"https?://docs\.google\.com/document/d/([\w-]+)")


def clean_topic(text: str | None) -> str | None:
    """Тема без пометки «[док]» и лишних пробелов; None, если тема пустая."""
    if not text:
        return None
    text = " ".join(DOC_MARKER.sub("", text).split())
    return text or None


def doc_id_from_url(url: str | None) -> str | None:
    match = DOC_URL.search(url or "")
    return match.group(1) if match else None


@dataclass(frozen=True)
class KpSlot:
    """Пост в календаре КП: день, слот (1 — основной, 2 — второй пост в пятой строке дня)."""

    date: date
    slot: int
    rubric: str | None
    status: str | None  # сырой статус из выпадающего списка; у второго поста его нет
    topic: str | None
    doc_url: str | None
    sheet: str
    cell: str  # ячейка темы (или рубрики, если темы ещё нет)

    @property
    def doc_id(self) -> str | None:
        return doc_id_from_url(self.doc_url)

    @property
    def key(self) -> tuple[date, int]:
        return (self.date, self.slot)


@dataclass(frozen=True)
class KpComment:
    """Комментарий в ячейке КП (ветка — корневой комментарий и ответы)."""

    id: str
    sheet: str
    cell: str
    author: str
    text: str
    created: datetime | None
    thread_id: str  # id корневого комментария ветки
    resolved: bool
    day: date | None = None  # к какому дню КП относится ячейка; None — ячейка вне календаря
    slot: int | None = None


@dataclass(frozen=True)
class SheetInfo:
    name: str
    month: date | None  # первое число месяца из названия листа
    hidden: bool
    weeks: int = 0
    used_weeks: int = 0  # сколько недель листа победили при слиянии дублей


@dataclass
class KpParseResult:
    slots: list[KpSlot] = field(default_factory=list)
    comments: list[KpComment] = field(default_factory=list)
    sheets: list[SheetInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unknown_statuses: set[str] = field(default_factory=set)
    # Дни, которые есть в календаре КП (даже если в них нет постов): нужно для прогресса КП.
    calendar_days: dict[date, str] = field(default_factory=dict)

    def slots_of(self, day: date) -> list[KpSlot]:
        return [s for s in self.slots if s.date == day]
