"""Обращение к боту по имени: «@бот, чё там по задачам?» — какую команду человек имел в виду.

Обычные вопросы бот без ИИ не разбирает, поэтому узнаёт только самые частые слова и подсказывает
команды. Работает лишь когда бота позвали по имени: в остальных разговорах он молчит.
"""

from __future__ import annotations

import re

from norm_tasker.tracker.stages import normalize

# Порядок важен: сначала узкие слова («доска», «кп»), самое общее — «что по задачам» — в конце.
ASKS: list[tuple[str, re.Pattern[str]]] = [
    (
        "help",
        re.compile(
            r"\bпомо[щг]\w*|\bкоманд\w*|\bчто (?:ты )?умеешь|\bкак (?:с тобой )?пользоваться"
        ),
    ),
    ("board", re.compile(r"\bдоск\w*")),
    ("kp", re.compile(r"\bкп\b|\bконтент-?план\w*")),
    ("design", re.compile(r"\bдизайн\w*|\bмакет\w*")),
    ("free", re.compile(r"\bсвободн\w*|\bне взят\w*|\bкто (?:возьмет|берет)")),
    ("my", re.compile(r"\bмои\w*|\bмоя\b|\bмое\b|\bу меня\b|\bмне\b|\bза мной\b")),
    ("today", re.compile(r"\bсегодня\b|\bгорит\b|\bгорят\b|\bсрочн\w*|\bдедлайн\w*")),
    (
        "week",
        re.compile(
            r"\bзадач\w*|\bпост\w*|\bнедел\w*|\bчто там\b|\bчто по\b|\bче по\b|\bчо по\b"
            r"|\bстатус\w*|\bкак дела\b|\bчто у нас\b|\bчто делаем\b"
        ),
    ),
]


def addressed_to(text: str | None, bot_username: str | None) -> bool:
    """Бота позвали по имени: в тексте есть @username (чужие @имена не считаются)."""
    if not text or not bot_username:
        return False
    return re.search(rf"@{re.escape(bot_username)}\b", text, re.IGNORECASE) is not None


def classify_ask(text: str, bot_username: str | None = None) -> str | None:
    """Имя команды бота, которую имел в виду автор; None — непонятно."""
    words = normalize(text)
    if bot_username:
        words = words.replace(f"@{normalize(bot_username)}", " ")
    for name, pattern in ASKS:
        if pattern.search(words):
            return name
    return None
