"""Обращение к боту: «@бот, чё там по задачам?» — какую команду человек имел в виду.

Обычные вопросы бот без ИИ не разбирает, поэтому узнаёт только самые частые слова и подсказывает
команды. Работает лишь когда к боту обратились — по имени или ответом на его сообщение: в остальных
разговорах он молчит.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from norm_tasker.tracker.stages import normalize

MAX_LISTEN_LENGTH = 600  # длинные сообщения — не отчёты о постах, ИИ их не читает
URL = re.compile(r"https?://\S+")
# Слова, по которым сообщение стоит показать ИИ: дальше решает он. Нарочно широко: лучше лишний
# запрос, чем пропущенный отчёт.
WORK_WORDS = re.compile(
    r"\d|пост|мем|текст|\bтем[аыуе]\b|дизайн|макет|клиент|заказчик|\bкп\b|контент|правк|готов|"
    r"отправ|отда[лю]|передал|скинул|показал|одобр|согласов|утвердил|\bок\b|окей|беру|возьму|"
    r"взял|вышел|вышла|вышло|опублик|выложил|запостил|перенес|перенос|успе|сделал|закончил|"
    r"написал|в работе|отложк|отложен|сторис|карточк|подборк|опрос|анонс|рилс"
)

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


# Слова, из которых состоит ответ «спасибо» или «понял»: такой ответ боту не вопрос.
# fmt: off
ACKNOWLEDGEMENTS = frozenset({
    "спасибо", "спс", "спасибочки", "пасиб", "благодарю", "большое", "огромное", "за", "ответ",
    "помощь", "вам", "тебе", "ок", "окей", "окэй", "окай", "оке", "ok", "okay", "ага", "угу",
    "понял", "поняла", "понятно", "ясно", "все", "принял", "приняла", "принято", "хорошо", "ладно",
    "отлично", "супер", "класс", "круто", "здорово", "норм", "нормально", "good", "thanks", "thx",
    "great", "nice", "хах", "хаха", "ахах", "ахаха",
})
# fmt: on


def addressed_to(text: str | None, bot_username: str | None) -> bool:
    """Бота позвали по имени: в тексте есть @username (чужие @имена не считаются)."""
    if not text or not bot_username:
        return False
    return re.search(rf"@{re.escape(bot_username)}\b", text, re.IGNORECASE) is not None


def bot_call_names(username: str | None, first_name: str | None) -> tuple[str, ...]:
    """Как зовут бота в начале фразы: «бот», имя из профиля, имя пользователя без «_bot»."""
    names = ["бот"]
    if first_name:
        names += [first_name, first_name.split()[0]]
    if username:
        stem = re.sub(r"_?bot$", "", username, flags=re.IGNORECASE)
        if stem:
            names.append(stem)
    return tuple(dict.fromkeys(name for name in names if name))


def called_by_name(text: str | None, names: Iterable[str]) -> bool:
    """Бота позвали в начале фразы: «бот, что по задачам?», «Ремайндберг, привет»."""
    stems = [re.escape(name) for name in (normalize(n) for n in names) if len(name) >= 3]
    if not text or not stems:
        return False
    norm = normalize(text)
    names_rx = "|".join(stems)
    found = re.match(rf"\W*(?:(?:эй|слушай|слушайте|привет|коллега)\W+)?(?:{names_rx})\b", norm)
    if not found:
        return False
    rest = norm[found.end() :].strip()
    # «Бот, …» и «бот: …» — обращение; «бот что горит?» — тоже; «бот сломался» — разговор о боте.
    return not rest or rest[0] in ",:!—–-" or rest.endswith("?")


def looks_like_work(text: str | None) -> bool:
    """Похоже ли сообщение на разговор о постах: номер, дата, слова про текст, клиента, этапы."""
    if not text or not 4 <= len(text) <= MAX_LISTEN_LENGTH or is_acknowledgement(text):
        return False
    return WORK_WORDS.search(normalize(URL.sub(" ", text))) is not None


def is_acknowledgement(text: str | None) -> bool:
    """Ответ вроде «спасибо», «ок» или «👍»: на него отвечать не нужно."""
    if not text or "?" in text:
        return False
    words = re.findall(r"[a-zа-я0-9]+", normalize(text))
    return all(word in ACKNOWLEDGEMENTS for word in words)


def classify_ask(text: str, bot_username: str | None = None) -> str | None:
    """Имя команды бота, которую имел в виду автор; None — непонятно."""
    words = normalize(text)
    if bot_username:
        words = words.replace(f"@{normalize(bot_username)}", " ")
    for name, pattern in ASKS:
        if pattern.search(words):
            return name
    return None
