"""Форматирование дат, постов и упоминаний для сообщений бота (HTML)."""

from __future__ import annotations

from datetime import date, datetime
from html import escape

from norm_tasker.tracker.models import Post
from norm_tasker.tracker.stages import EMOJI, LABEL, Stage

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAYS_LONG = [
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
]  # fmt: skip
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]  # fmt: skip
MONTHS_NOM = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]  # fmt: skip


def wd_date(day: date) -> str:
    """пт 16.10"""
    return f"{WEEKDAYS[day.weekday()]} {day:%d.%m}"


def dt_short(moment: datetime) -> str:
    """пт 16.10 18:00"""
    return f"{wd_date(moment.date())} {moment:%H:%M}"


def long_date(day: date) -> str:
    """Вторник, 13 октября"""
    return f"{WEEKDAYS_LONG[day.weekday()].capitalize()}, {day.day} {MONTHS_GEN[day.month - 1]}"


def clip(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def topic(post: Post, limit: int = 60) -> str:
    return escape(clip(post.title, limit))


def label(post: Post, limit: int = 60) -> str:
    """№12 «Тема»"""
    return f"№{post.id} «{topic(post, limit)}»"


def line(post: Post, limit: int = 60) -> str:
    """пт 16.10 · №12 «Тема»"""
    return f"{wd_date(post.publish_date)} · {label(post, limit)}"


def stage_text(stage: Stage) -> str:
    return f"{EMOJI[stage]} {LABEL[stage]}"


def author(post: Post) -> str:
    return escape(post.assignee_name) if post.assignee_name else "никто не взял"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)
