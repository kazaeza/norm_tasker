"""Что человек хотел сказать боту: спросить, записать в трекер или просто поговорил.

Gemini получает сообщение вместе со сводкой по постам и отвечает JSON-объектом. Здесь ответ
разбирается и проверяется. Бот исполняет только то, что можно сверить с трекером (посты есть,
этапы и даты настоящие), остальное отбрасывает: модель может ошибиться или выдумать.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

from norm_tasker.tracker.stages import Stage

log = logging.getLogger(__name__)

MAX_ACTIONS = 4  # больше действий из одного сообщения не выполняем
MAX_POSTS = 6  # и больше постов в одном действии
TOPIC_LIMIT = 200

KINDS = ("answer", "action", "clarify", "ignore")
ACTION_TYPES = ("claim", "release", "stage", "rollback", "move", "not_post", "add_post")
# Что бот записывает из сообщения, которое ему не адресовано: самое частое и самое безобидное.
LISTEN_TYPES = ("claim", "release", "stage")
STAGE_NAMES = {stage.name.lower(): stage for stage in Stage if stage > Stage.NEW}

FENCE = re.compile(r"^```[a-z]*\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
NUMERIC_DATE = re.compile(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?")


@dataclass(frozen=True)
class Action:
    """Одно действие над трекером. Поля, которые не нужны этому действию, пустые."""

    type: str
    posts: tuple[int, ...] = ()
    stage: Stage | None = None
    day: date | None = None
    topic: str | None = None


@dataclass(frozen=True)
class Interpretation:
    """Как бот понял сообщение."""

    kind: str = "ignore"  # answer | action | clarify | ignore
    text: str = ""  # ответ человеку (answer) или уточняющий вопрос (clarify)
    confident: bool = False  # модель уверена, что пост и этап названы однозначно
    actions: tuple[Action, ...] = ()


def _json_object(raw: str) -> dict | None:
    """Первый JSON-объект из ответа: модель иногда оборачивает его в ``` или пишет вступление."""
    text = raw.strip()
    fenced = FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    candidates = [text]
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{") : text.rindex("}") + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _post_numbers(value: object) -> tuple[int, ...]:
    """Номера постов: 12, "12", "№12" или список таких значений."""
    items = value if isinstance(value, list) else [value]
    numbers: list[int] = []
    for item in items:
        if isinstance(item, bool) or item is None:
            continue
        found = re.search(r"\d+", str(item))
        if found and int(found.group()) not in numbers:
            numbers.append(int(found.group()))
    return tuple(numbers[:MAX_POSTS])


def _day(value: object, today: date) -> date | None:
    """Дата из «2026-10-20» или «20.10» (год не назван — ближайший подходящий)."""
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    found = NUMERIC_DATE.fullmatch(text)
    if not found:
        return None
    day, month = int(found.group(1)), int(found.group(2))
    year = int(found.group(3)) if found.group(3) else today.year
    if year < 100:
        year += 2000
    try:
        result = date(year, month, day)
    except ValueError:
        return None
    if not found.group(3) and result < today - timedelta(days=60):
        result = result.replace(year=result.year + 1)
    return result


def parse_action(raw: object, today: date) -> Action | None:
    """Действие из ответа модели; None, если оно неполное или неизвестное."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in ACTION_TYPES:
        return None
    posts = _post_numbers(raw.get("posts", raw.get("post")))
    stage = STAGE_NAMES.get(str(raw.get("stage") or "").strip().lower())
    day = _day(raw.get("date"), today)
    topic = " ".join(str(raw.get("topic") or "").split())[:TOPIC_LIMIT] or None
    if kind == "add_post":
        return Action(kind, day=day, topic=topic) if day else None
    if not posts:
        return None
    if kind in ("stage", "rollback") and stage is None:
        return None
    if kind == "move" and day is None:
        return None
    return Action(kind, posts, stage, day, topic)


def parse_interpretation(raw: str, today: date) -> Interpretation:
    """Разбирает ответ модели. Ответ не JSON — считаем его обычным текстом-ответом."""
    data = _json_object(raw)
    if data is None:
        text = raw.strip()
        if not text or text.startswith(("{", "```")):
            return Interpretation()  # JSON оборвался: обрывок людям показывать нельзя
        return Interpretation("answer", text)
    listed = data.get("actions")
    items = listed[:MAX_ACTIONS] if isinstance(listed, list) else []
    actions = tuple(a for a in (parse_action(item, today) for item in items) if a is not None)
    text = str(data.get("text") or "").strip()
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in KINDS:
        kind = "action" if actions else "answer" if text else "ignore"
    if kind == "action" and not actions:
        kind = "answer" if text else "ignore"
    confident = str(data.get("confidence") or "").strip().lower() == "high"
    return Interpretation(kind, text, confident, actions)
