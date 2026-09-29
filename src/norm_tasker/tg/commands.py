"""Разбор команд бота: «/post@my_bot 12» → имя, адресат, аргументы."""

from __future__ import annotations

import re
from dataclasses import dataclass

COMMAND = re.compile(r"/(?P<name>\w+)(?:@(?P<mention>\w+))?(?:\s+(?P<args>.*))?", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Command:
    name: str  # без «/» и в нижнем регистре
    args: str  # всё после команды, без пробелов по краям; пусто, если аргументов нет
    mention: str | None = None  # бот, которому адресована команда: /help@some_bot


def parse_command(text: str | None, bot_username: str | None = None) -> Command | None:
    """Команда должна стоять в начале сообщения. Адресованная другому боту — не наша."""
    if not text:
        return None
    found = COMMAND.fullmatch(text.strip())
    if found is None:
        return None
    mention = found["mention"]
    if mention and bot_username and mention.lower() != bot_username.lower():
        return None
    return Command(found["name"].lower(), (found["args"] or "").strip(), mention)
