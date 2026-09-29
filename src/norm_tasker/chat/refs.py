"""Как в сообщении названы посты: номер, дата, день недели, рубрика, слова из темы."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from norm_tasker.kp.parser import RU_MONTHS_GENITIVE
from norm_tasker.tracker.stages import normalize

WEEKDAY_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    (0, re.compile(r"\bпонедельник\w*")),
    (1, re.compile(r"\bвторник\w*")),
    (2, re.compile(r"\bсред(?:а|у|ы|е|ой)\b")),
    (3, re.compile(r"\bчетверг\w*")),
    (4, re.compile(r"\b(?:пятниц|пятничн)\w*")),
    (5, re.compile(r"\b(?:суббот|субботн)\w*")),
    (6, re.compile(r"\b(?:воскресень|воскресн)\w*")),
]
WEEKDAY_ABBR = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
ABBR = re.compile(r"\b(?:на|в|во)\s+(пн|вт|ср|чт|пт|сб|вс)\b")
RELATIVE = [
    (re.compile(r"\bпослезавтра\w*"), 2),
    (re.compile(r"\bзавтра\w*"), 1),
    (re.compile(r"\bсегодня\w*"), 0),
    (re.compile(r"\bвчера\w*"), -1),
]
NUMBER = re.compile(r"(?:№|#|\bномер\s+)\s*(\d+)\b|\bпост\w*\s+(\d+)\b(?![./]\d)")
DATE_NUMERIC = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
MONTHS = "|".join(RU_MONTHS_GENITIVE)
DATE_WORDS = re.compile(rf"\b(\d{{1,2}})\s+({MONTHS})(?:\s+(\d{{4}}))?\b")
DAY_ORDINAL = re.compile(r"\bна\s+(\d{1,2})-?(?:е|го|ое|ого)\b")
DAY_LAST = re.compile(r"\bна\s+(\d{1,2})\s*[.!,;]?\s*$")

RUBRICS = [
    ("Продукт", re.compile(r"\bпродукт\w*")),
    ("Развлекательный", re.compile(r"\bразвлекат\w*")),
    ("Информационный", re.compile(r"\b(?:информац|информат|инфо)\w*")),
]
STOPWORDS = {
    "беру", "возьму", "заберу", "пост", "посты", "постик", "текст", "тему", "тема", "темы",
    "клиенту", "клиент", "клиента", "готов", "готово", "отправил", "отправила", "показал",
    "показала", "дизайн", "дизайнеру", "дизайнера", "финальный", "вышел", "вышла", "который",
    "которая", "тоже", "этот", "этого", "себе", "будет", "пожалуйста", "сегодня", "завтра",
    "пятницу", "четверг", "среду", "вторник", "понедельник", "субботу", "воскресенье",
    "продуктовый", "развлекательный", "информационный", "выложил", "выложила", "опубликовал",
    "опубликовала", "перенесли", "перенести", "переносим", "отдал", "отдала", "передал",
    "передала", "клиентом", "согласовал", "согласовала", "правки", "правкам",
}  # fmt: skip
STEM = 5


@dataclass(frozen=True)
class PostRef:
    numbers: tuple[int, ...] = ()
    dates: tuple[date, ...] = ()
    weekdays: tuple[int, ...] = ()
    day_numbers: tuple[int, ...] = ()  # «на 16» — число месяца без названия
    rubrics: tuple[str, ...] = ()
    words: tuple[str, ...] = ()  # слова, которые могли быть из темы поста

    @property
    def has_date(self) -> bool:
        return bool(self.dates or self.weekdays or self.day_numbers)

    @property
    def is_empty(self) -> bool:
        return not (self.numbers or self.has_date or self.rubrics or self.words)


def _year_for(day: int, month: int, today: date) -> int:
    """Год для даты без года: тот, при котором дата ближе всего к сегодняшнему дню."""
    best_year, best_gap = today.year, None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            gap = abs((date(year, month, day) - today).days)
        except ValueError:
            continue
        if best_gap is None or gap < best_gap:
            best_year, best_gap = year, gap
    return best_year


def _make_date(day: int, month: int, year: int | None, today: date) -> date | None:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if year is None:
        year = _year_for(day, month, today)
    elif year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_dates(text: str, today: date) -> list[tuple[int, int, date]]:
    """Все даты в тексте: (начало, конец, дата). Текст должен быть нормализован."""
    found: list[tuple[int, int, date]] = []
    for match in DATE_WORDS.finditer(text):
        month = RU_MONTHS_GENITIVE.index(match.group(2)) + 1
        year = int(match.group(3)) if match.group(3) else None
        day = _make_date(int(match.group(1)), month, year, today)
        if day:
            found.append((match.start(), match.end(), day))
    for match in DATE_NUMERIC.finditer(text):
        year = int(match.group(3)) if match.group(3) else None
        day = _make_date(int(match.group(1)), int(match.group(2)), year, today)
        if day:
            found.append((match.start(), match.end(), day))
    return sorted(found)


def extract_refs(text: str, today: date, ignore: tuple[str, ...] = ()) -> PostRef:
    """Разбирает, какие посты названы в сообщении. ignore — основы имён, не относящихся к теме."""
    text = normalize(text)
    numbers = [int(a or b) for a, b in NUMBER.findall(text)]
    spans = [m.span() for m in NUMBER.finditer(text)]

    dates: list[date] = []
    for start, end, day in find_dates(text, today):
        dates.append(day)
        spans.append((start, end))

    weekdays: list[int] = []
    for weekday, pattern in WEEKDAY_PATTERNS:
        for match in pattern.finditer(text):
            weekdays.append(weekday)
            spans.append(match.span())
    for match in ABBR.finditer(text):
        weekdays.append(WEEKDAY_ABBR[match.group(1)])
        spans.append(match.span())
    for pattern, offset in RELATIVE:
        for match in pattern.finditer(text):
            dates.append(today + timedelta(days=offset))
            spans.append(match.span())

    day_numbers: list[int] = []
    for pattern in (DAY_ORDINAL, DAY_LAST):
        match = pattern.search(text)
        if match and not dates:
            day_numbers.append(int(match.group(1)))
            spans.append(match.span())

    rubrics = []
    for name, pattern in RUBRICS:
        for match in pattern.finditer(text):
            rubrics.append(name)
            spans.append(match.span())

    rest = list(text)
    for start, end in spans:
        for i in range(start, end):
            rest[i] = " "
    candidates = re.findall(r"[а-яa-z]{4,}", "".join(rest))
    words = tuple(
        dict.fromkeys(
            w
            for w in candidates
            if w not in STOPWORDS and not any(w.startswith(stem) for stem in ignore)
        )
    )
    return PostRef(
        numbers=tuple(dict.fromkeys(numbers)),
        dates=tuple(dict.fromkeys(dates)),
        weekdays=tuple(dict.fromkeys(weekdays)),
        day_numbers=tuple(dict.fromkeys(day_numbers)),
        rubrics=tuple(dict.fromkeys(rubrics)),
        words=words,
    )


def stems(text: str | None) -> set[str]:
    return {w[:STEM] for w in re.findall(r"[а-яa-z]{4,}", normalize(text))}


def topic_score(words: tuple[str, ...], topic: str | None) -> int:
    """Сколько слов из сообщения совпало с темой (по первым пяти буквам)."""
    have = stems(topic)
    return sum(1 for w in words if len(w) >= STEM and w[:STEM] in have)
