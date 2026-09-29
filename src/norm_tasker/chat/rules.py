"""Распознавание сообщений команды без ИИ.

Правила намеренно осторожные: бот читает весь чат, поэтому лучше пропустить
нестандартную фразу, чем сработать на разговор. Всё, что бот понял, он подтверждает,
а сомнительное выбирает кнопками.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from norm_tasker.chat.refs import (
    DATE_NUMERIC,
    DATE_WORDS,
    MONTHS,
    WEEKDAY_PATTERNS,
    PostRef,
    extract_refs,
    find_dates,
)
from norm_tasker.tracker.stages import Stage, normalize

MAX_WORDS = 30

OK = r"(?:ок|ok|окей|окэй|окай|оке|okay)"
SEND = r"(?:отправил|скинул|показал|отдал|передал|переслал|выслал|послал|отослал|направил)[аои]?"
CLIENT = r"(?:клиент\w*|марго|дарь(?:я|е|и|ю|ей)|заказчик\w*)"
TOKENS = {"<OK>": OK, "<SEND>": SEND, "<CLIENT>": CLIENT}


def rx(pattern: str) -> re.Pattern[str]:
    for token, value in TOKENS.items():
        pattern = pattern.replace(token, value)
    return re.compile(pattern)


class IntentKind(StrEnum):
    CLAIM = "claim"  # «беру пост на пятницу»
    ASSIGN = "assign"  # «Даня, возьми пост на среду»
    RELEASE = "release"  # «не успеваю пост на среду»
    STAGE = "stage"  # «текст готов», «вышел»
    CLIENT_OK = "client_ok"  # «клиент ок» — по тексту или финальный, решает этап поста
    SHOWN_CLIENT = "shown_client"  # «показал клиенту» — текст или дизайн, решает этап поста
    ROLLBACK = "rollback"  # «клиент вернул текст на правки»
    MOVE = "move"  # «пост про … переносим на 20.10»
    NOT_POST = "not_post"  # «это не пост»
    KP_OK = "kp_ok"  # «кп окей»
    KP_SHOWN = "kp_shown"  # «показали кп клиенту»
    KP_OWN = "kp_own"  # «беру кп»


@dataclass(frozen=True)
class Intent:
    kind: IntentKind
    ref: PostRef
    stage: Stage | None = None
    target: str | None = None  # ASSIGN: кому предложили взять пост
    new_date: date | None = None  # MOVE: на какую дату
    new_weekday: int | None = None  # MOVE: «на понедельник» — дата считается от даты поста
    rule: str = ""  # какое правило сработало (для журнала и отладки)


# Правила этапов. Из совпавших берётся самый поздний этап: «клиент ок, отдал дизайнеру»
# означает «у дизайнера».
STAGE_RULES: list[tuple[Stage, list[re.Pattern[str]]]] = [
    (
        Stage.PUBLISHED,
        [
            rx(r"\b(?:вышел|вышла|вышло)\b"),
            rx(r"\bопубликова(?:л|ла|ли|н|на|но)\b"),
            rx(r"\bвыложил[аи]?\b|\bвыложен[аоы]?\b"),
            rx(r"\bзапостил[аи]?\b"),
            rx(r"\b(?:поставил|поставила|поставили|запланировал|запланировала)\b.*\bотлож\w*"),
            rx(r"\bв отложк\w*|\bв отложенн\w*|\bотложк\w+ (?:стоит|готова|поставлена)"),
        ],
    ),
    (
        Stage.FINAL_OK,
        [
            rx(r"\bфинальн\w*\s+<OK>\b"),
            rx(r"\bфинальн\w*\s+(?:одобрен|согласован|одобрение|согласование)\w*"),
            rx(r"\bфин\.?\s*<OK>\b"),
            rx(r"\bфинально\s+(?:одобр|согласов)\w*"),
            rx(r"\bпост\s+(?:согласован|одобрен|утвержден)\w*"),
            rx(r"\bпо дизайну\s+<OK>\b|\b<OK>\s+по дизайну\b"),
            rx(r"\bдизайн\s+(?:согласован|одобрен|утвержден)\w*"),
            rx(r"\bможно\s+(?:постить|публиковать|выкладывать)\b"),
        ],
    ),
    (
        Stage.SHOWN_DESIGN,
        [
            rx(
                r"\b<SEND>\s+(?:\S+\s+){0,4}?<CLIENT>\b.*"
                r"\b(?:дизайн\w*|макет\w*|картинк\w*|визуал\w*|готов\w+ пост\w*)"
            ),
            rx(
                r"\b<SEND>\s+(?:\S+\s+){0,3}?(?:дизайн\w*|макет\w*|готов\w+ пост\w*)"
                r"\s+(?:\S+\s+){0,3}?<CLIENT>\b"
            ),
            rx(r"\bпост\s+(?:показан|отправлен)\w*\s+клиент\w*"),
            rx(r"\bдизайн\s+(?:у клиента|на согласовании|показан\w*)"),
        ],
    ),
    (
        Stage.DESIGN_READY,
        [
            rx(r"\bдизайн\s+(?:готов\w*|есть|пришел|пришла|прислали|сделан\w*|отрисован\w*)"),
            rx(r"\b(?:макет|макеты|картинк\w*|визуал\w*)\s+(?:готов\w*|пришел|пришли|есть)\b"),
            rx(
                r"\bдизайнер(?:ом)?\s+(?:прислал\w*|отрисовал\w*|сделал\w*|нарисовал\w*"
                r"|скинул\w*|отдал\w*)"
            ),
            rx(r"\bотрисовал[аи]?\b|\bотрисован\w*"),
            rx(r"\b(?:пришел дизайн|пришли макеты)\b"),
        ],
    ),
    (
        Stage.AT_DESIGNER,
        [
            rx(r"\b<SEND>\s+(?:\S+\s+){0,3}?дизайнеру\b"),
            rx(r"\bдизайнеру\s+<SEND>"),
            rx(r"\bу дизайнера\b"),
            rx(r"\b<SEND>\s+(?:\S+\s+){0,2}?на дизайн\b"),
            rx(r"\bдок\w*\s+передан\w*|\bпередан\w*\s+дизайнеру"),
        ],
    ),
    (
        Stage.TEXT_OK,
        [
            rx(
                r"\bпо (?:тексту|мему|идее|теме)\b.*\b<OK>\b"
                r"|\b<OK>\b.*\bпо (?:тексту|мему|идее|теме)\b"
            ),
            rx(
                r"\bпо (?:тексту|мему|идее|теме)\b.*\b(?:одобр|согласов|утверд|принял)\w*"
                r"|\b(?:одобр|согласов|утверд|принял)\w*.*\bпо (?:тексту|мему|идее|теме)\b"
            ),
            rx(r"\bтекст\w*\s+(?:согласован|одобрен|утвержден|принят)\w*"),
            rx(r"\bтекст\w*\s+<OK>\b"),
            rx(
                r"\b(?:окнул|окнули|одобрил|одобрили|согласовал|согласовали|утвердил|утвердили)[аи]?"
                r"\s+(?:\S+\s+){0,2}?(?:текст|идею|мем)\w*"
            ),
            rx(r"\bтекст\s+(?:прошел|зашел)\b"),
        ],
    ),
    (
        Stage.TEXT_SHOWN,
        [
            rx(r"\b(?:текст|идея|идею|мем)\w*\s+(?:\S+\s+){0,3}?(?:готов|написан)\w*"),
            rx(
                r"\b<SEND>\s+(?:\S+\s+){0,3}?(?:текст|идею|идея|мем)\w*\s+(?:\S+\s+){0,2}?<CLIENT>\b"
            ),
            rx(r"\b<SEND>\s+(?:\S+\s+){0,2}?<CLIENT>\s+(?:\S+\s+){0,2}?(?:текст|идею|идея|мем)\w*"),
            rx(r"\bтекст\w*\s+(?:у клиента|на согласовании|отправлен\w*|показан\w*)"),
            rx(r"\bна согласовани[ие]\b"),
        ],
    ),
]
CLIENT_OK_RULES = [
    rx(r"\b<CLIENT>\s+(?:\S+\s+)?<OK>\b"),
    rx(r"\b<OK>\s+от\s+<CLIENT>\b"),
    rx(r"\b<CLIENT>\s+(?:одобрил|согласовал|утвердил|принял)\w*"),
]
SHOWN_CLIENT_RULE = rx(r"\b<SEND>\s+(?:\S+\s+){0,2}?<CLIENT>\b")
NOT_SENT = rx(r"\bне\s+(?:\S+\s+)?(?:отправ|показ|скин|отда|перед|выслал)\w*")
FUTURE = rx(
    r"\b(?:будет|буду|будут|сделаю|напишу|отправлю|покажу|отдам|скоро|позже|потом|попозже|"
    r"собираюсь|планирую)\b"
)
LATE_STAGES = {Stage.TEXT_SHOWN, Stage.AT_DESIGNER, Stage.SHOWN_DESIGN}

CLAIM_RULES = [
    rx(r"\b(?:беру|возьму|заберу)\b"),
    rx(
        r"\b(?:взял|взяла|взяли)\s+(?:себе\s+)?(?:пост|мем|тему|продукт\w*|развлекат\w*|информац\w*)"
    ),
]
RELEASE_STRONG = rx(r"\bне\s+(?:успеваю|успею|успеем|смогу|получится|получается)\b")
RELEASE_VERBS = rx(r"\b(?:отдаю|снимаю|освобождаю|сбрасываю|отказываюсь)\b")
ASSIGN_RULES = [
    rx(
        r"^(?P<who>@?[а-яa-z_0-9]{2,})[,:!]?\s+(?:ты\s+)?"
        r"(?:возьми|возьмешь|берешь|займись|займешься|сделаешь|напишешь|напиши|сделай)\b"
    ),
    rx(r"\bпусть\s+(?P<who>@?[а-яa-z_0-9]{2,})\s+(?:возьмет|берет|сделает|напишет|займется)\b"),
    rx(r"(?P<who>@[a-z_0-9]{3,})\s+(?:возьми|берешь|возьмешь|займись|займешься)\b"),
]
ROLLBACK_RULES = [
    rx(r"\b(?:вернул\w*|вернули|вернуло)\s+(?:\S+\s+){0,3}?на\s+правк\w*"),
    rx(r"\bправки\s+(?:от|по)\s+<CLIENT>\b"),
    rx(
        r"\b<CLIENT>\s+(?:не\s+(?:согласовал|одобрил|принял)\w*|отклонил\w*|забраковал\w*|"
        r"попросил\w*\s+(?:переделать|поправить|исправить))"
    ),
    rx(r"\bна доработку\b"),
]
MOVE_VERB = rx(r"\b(?:перенос\w*|перенес\w*|сдвин\w*|сдвига\w*|переставь\w*|переставл\w*)\b")
MOVE_TARGET = re.compile(
    r"\bна\s+(?P<t>\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?"
    rf"|\d{{1,2}}\s+(?:{MONTHS})(?:\s+\d{{4}})?"
    r"|понедельник\w*|вторник\w*|сред[ауы]|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*"
    r"|завтра|послезавтра)"
)
NOT_POST_RULE = rx(r"\b(?:это\s+)?не\s+пост\w*\b|\bне\s+является\s+постом\b")
KP_WORD = rx(r"\bкп\b|\bконтент-?план\w*")
KP_OK_RULES = [
    rx(r"\b(?:кп|контент-?план\w*)\b.*\b(?:<OK>|согласован\w*|утвержден\w*|одобрен\w*)\b"),
    rx(r"\b(?:окнул|окнули|согласовал|согласовали|утвердил|утвердили|одобрил|одобрили)[аи]?\s+"
       r"(?:\S+\s+){0,2}?(?:кп|контент-?план\w*)\b"),
    rx(r"\b<OK>\s+(?:по|на)\s+(?:кп|контент-?план\w*)"),
]  # fmt: skip
KP_SHOWN_RULES = [
    rx(r"\b<SEND>\s+(?:\S+\s+){0,3}?(?:кп|контент-?план\w*)\b"),
    rx(r"\b(?:кп|контент-?план\w*)\s+(?:показан|отправлен|у клиента)\w*"),
]
KP_OWN_RULE = rx(r"\b(?:беру|возьму)\s+(?:сборку\s+)?(?:кп|контент-?план\w*)\b")
URL = re.compile(r"https?://\S+")


def _negated(text: str, match: re.Match[str]) -> bool:
    window = text[max(0, match.start() - 14) : match.end()]
    return re.search(r"\bне\b", window) is not None


def _first(patterns: list[re.Pattern[str]], text: str) -> re.Match[str] | None:
    for pattern in patterns:
        for match in pattern.finditer(text):
            if not _negated(text, match):
                return match
    return None


def _weekday_of(text: str) -> int | None:
    for weekday, pattern in WEEKDAY_PATTERNS:
        if pattern.search(text):
            return weekday
    return None


def _move(norm: str, today: date) -> Intent | None:
    if not MOVE_VERB.search(norm):
        return None
    matches = list(MOVE_TARGET.finditer(norm))
    if not matches:
        return None
    target = matches[-1]
    text = target.group("t")
    new_date: date | None = None
    new_weekday: int | None = None
    if text in ("завтра", "послезавтра"):
        new_date = today + timedelta(days=1 if text == "завтра" else 2)
    elif DATE_NUMERIC.fullmatch(text) or DATE_WORDS.fullmatch(text):
        dates = find_dates(text, today)
        new_date = dates[0][2] if dates else None
    else:
        new_weekday = _weekday_of(text)
    if new_date is None and new_weekday is None:
        return None
    rest = norm[: target.start()] + " " + norm[target.end() :]
    return Intent(
        IntentKind.MOVE,
        extract_refs(rest, today),
        new_date=new_date,
        new_weekday=new_weekday,
        rule="move",
    )


def parse_message(text: str, today: date) -> Intent | None:
    """Что хотел сказать автор сообщения; None, если сообщение не про посты."""
    if not text or text.lstrip().startswith("/"):
        return None
    norm = normalize(URL.sub(" ", text))
    if not norm or len(norm.split()) > MAX_WORDS:
        return None
    question = "?" in norm

    # КП.
    if KP_WORD.search(norm) and not question:
        if KP_OWN_RULE.search(norm):
            return Intent(IntentKind.KP_OWN, PostRef(), rule="kp_own")
        if _first(KP_SHOWN_RULES, norm):
            return Intent(IntentKind.KP_SHOWN, PostRef(), rule="kp_shown")
        if _first(KP_OK_RULES, norm):
            return Intent(IntentKind.KP_OK, PostRef(), rule="kp_ok")

    if NOT_POST_RULE.search(norm):
        return Intent(IntentKind.NOT_POST, extract_refs(norm, today), rule="not_post")

    if not question:
        move = _move(norm, today)
        if move:
            return move

        if _first(ROLLBACK_RULES, norm) and not FUTURE.search(norm):
            design = re.search(r"\b(?:дизайн\w*|макет\w*|картинк\w*)", norm)
            return Intent(
                IntentKind.ROLLBACK,
                extract_refs(norm, today),
                stage=Stage.AT_DESIGNER if design else Stage.TAKEN,
                rule="rollback",
            )

    looks_like_post = (
        bool(re.search(r"\b(?:пост|мем|тем)\w*", norm)) or extract_refs(norm, today).has_date
    )
    wants_release = RELEASE_STRONG.search(norm) or RELEASE_VERBS.search(norm)
    if wants_release and looks_like_post and "дизайнер" not in norm:
        return Intent(IntentKind.RELEASE, extract_refs(norm, today), rule="release")

    for pattern in ASSIGN_RULES:
        match = pattern.search(norm)
        if match and not question:
            return Intent(
                IntentKind.ASSIGN,
                extract_refs(pattern.sub(" ", norm, count=1), today),
                target=match.group("who").lstrip("@"),
                rule="assign",
            )

    if question:
        return None

    if _first(CLAIM_RULES, norm):
        return Intent(IntentKind.CLAIM, extract_refs(norm, today), rule="claim")

    if FUTURE.search(norm):
        return None

    stages: list[tuple[Stage, str]] = []
    for stage, patterns in STAGE_RULES:
        if _first(patterns, norm):
            stages.append((stage, f"stage:{stage.name.lower()}"))
    if NOT_SENT.search(norm):
        stages = [(s, r) for s, r in stages if s not in LATE_STAGES]
    if stages:
        stage, rule = max(stages, key=lambda item: item[0])
        return Intent(IntentKind.STAGE, extract_refs(norm, today), stage=stage, rule=rule)

    if _first(CLIENT_OK_RULES, norm):
        return Intent(IntentKind.CLIENT_OK, extract_refs(norm, today), rule="client_ok")
    if _first([SHOWN_CLIENT_RULE], norm) and not NOT_SENT.search(norm):
        return Intent(IntentKind.SHOWN_CLIENT, extract_refs(norm, today), rule="shown_client")
    return None
