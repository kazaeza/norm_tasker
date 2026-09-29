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
from functools import lru_cache

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
TOKENS = {"<OK>": OK, "<SEND>": SEND}


def late(pattern: str) -> str:
    """Шаблон с подставленными общими словами. Слово <CLIENT> подставляется при сборке правил."""
    for token, value in TOKENS.items():
        pattern = pattern.replace(token, value)
    return pattern


def rx(pattern: str) -> re.Pattern[str]:
    return re.compile(late(pattern))


def name_stem(name: str) -> str:
    """Основа имени для поиска в любом падеже: «Алиса» → «алис»."""
    words = normalize(name).split()
    stem = words[0] if words else ""
    return stem[:-1] if len(stem) >= 4 and stem[-1] in "аяьй" else stem


def client_words(names: tuple[str, ...]) -> str:
    """Кому уходит текст: клиент, заказчик и люди клиента по именам из настроек."""
    stems = [stem for stem in (name_stem(n) for n in names) if stem]
    return r"(?:клиент\w*|заказчик\w*" + "".join(f"|{re.escape(s)}\\w*" for s in stems) + ")"


class IntentKind(StrEnum):
    CLAIM = "claim"  # «беру пост на пятницу»
    ASSIGN = "assign"  # «Вася, возьми пост на среду»
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
STAGE_RULE_SOURCES: list[tuple[Stage, list[str]]] = [
    (
        Stage.PUBLISHED,
        [
            late(r"\bопубликова(?:л|ла|ли|н|на|но)\b"),
            late(r"\bвыложил[аи]?\b|\bвыложен[аоы]?\b"),
            late(r"\bзапостил[аи]?\b"),
            late(r"\b(?:поставил|поставила|поставили|запланировал|запланировала)\b.*\bотлож\w*"),
            late(r"\bв отложк\w*|\bв отложенн\w*|\bотложк\w+ (?:стоит|готова|поставлена)"),
        ],
    ),
    (
        Stage.FINAL_OK,
        [
            late(r"\bфинальн\w*\s+<OK>\b"),
            late(r"\bфинальн\w*\s+(?:одобрен|согласован|одобрение|согласование)\w*"),
            late(r"\bфин\.?\s*<OK>\b"),
            late(r"\bфинально\s+(?:одобр|согласов)\w*"),
            late(r"\bпост\s+(?:согласован|одобрен|утвержден)\w*"),
            late(r"\bпо дизайну\s+<OK>\b|\b<OK>\s+по дизайну\b"),
            late(r"\bдизайн\s+(?:согласован|одобрен|утвержден)\w*"),
            late(r"\bможно\s+(?:постить|публиковать|выкладывать)\b"),
        ],
    ),
    (
        Stage.SHOWN_DESIGN,
        [
            late(
                r"\b<SEND>\s+(?:\S+\s+){0,4}?<CLIENT>\b.*"
                r"\b(?:дизайн\w*|макет\w*|картинк\w*|визуал\w*|готов\w+ пост\w*)"
            ),
            late(
                r"\b<SEND>\s+(?:\S+\s+){0,3}?(?:дизайн\w*|макет\w*|готов\w+ пост\w*)"
                r"\s+(?:\S+\s+){0,3}?<CLIENT>\b"
            ),
            late(r"\bпост\s+(?:показан|отправлен)\w*\s+клиент\w*"),
            late(r"\bдизайн\s+(?:у клиента|на согласовании|показан\w*)"),
        ],
    ),
    (
        Stage.DESIGN_READY,
        [
            late(r"\bдизайн\s+(?:готов\w*|есть|пришел|пришла|прислали|сделан\w*|отрисован\w*)"),
            late(r"\b(?:макет|макеты|картинк\w*|визуал\w*)\s+(?:готов\w*|пришел|пришли|есть)\b"),
            late(
                r"\bдизайнер(?:ом)?\s+(?:прислал\w*|отрисовал\w*|сделал\w*|нарисовал\w*"
                r"|скинул\w*|отдал\w*)"
            ),
            late(r"\bотрисовал[аи]?\b|\bотрисован\w*"),
            late(r"\b(?:пришел дизайн|пришли макеты)\b"),
        ],
    ),
    (
        Stage.AT_DESIGNER,
        [
            late(r"\b<SEND>\s+(?:\S+\s+){0,3}?дизайнеру\b"),
            late(r"\bдизайнеру\s+<SEND>"),
            late(r"\bу дизайнера\b"),
            late(r"\b<SEND>\s+(?:\S+\s+){0,2}?на дизайн\b"),
            late(r"\bдок\w*\s+передан\w*|\bпередан\w*\s+дизайнеру"),
        ],
    ),
    (
        Stage.TEXT_OK,
        [
            late(
                r"\bпо (?:тексту|мему|идее|теме)\b.*\b<OK>\b"
                r"|\b<OK>\b.*\bпо (?:тексту|мему|идее|теме)\b"
            ),
            late(
                r"\bпо (?:тексту|мему|идее|теме)\b.*\b(?:одобр|согласов|утверд|принял)\w*"
                r"|\b(?:одобр|согласов|утверд|принял)\w*.*\bпо (?:тексту|мему|идее|теме)\b"
            ),
            late(r"\bтекст\w*\s+(?:согласован|одобрен|утвержден|принят)\w*"),
            late(r"\bтекст\w*\s+<OK>\b"),
            late(
                r"\b(?:окнул|окнули|одобрил|одобрили|согласовал|согласовали|утвердил|утвердили)[аи]?"
                r"\s+(?:\S+\s+){0,2}?(?:текст|идею|мем)\w*"
            ),
            late(r"\bтекст\s+(?:прошел|зашел)\b"),
        ],
    ),
    (
        Stage.TEXT_SHOWN,
        [
            late(r"\b(?:текст|идея|идею|мем)\w*\s+(?:\S+\s+){0,3}?(?:готов|написан)\w*"),
            late(
                r"\b<SEND>\s+(?:\S+\s+){0,3}?(?:текст|идею|идея|мем)\w*\s+(?:\S+\s+){0,2}?<CLIENT>\b"
            ),
            late(
                r"\b<SEND>\s+(?:\S+\s+){0,2}?<CLIENT>\s+(?:\S+\s+){0,2}?(?:текст|идею|идея|мем)\w*"
            ),
            late(r"\b(?:текст|идея|идею|мем)\w*\s+<SEND>\s+(?:\S+\s+){0,2}?<CLIENT>\b"),
            late(r"\bтекст\w*\s+(?:у клиента|на согласовании|отправлен\w*|показан\w*)"),
            late(r"\bна согласовани[ие]\b"),
        ],
    ),
]
CLIENT_OK_SOURCES = [
    late(r"\b<CLIENT>\s+(?:\S+\s+)?<OK>\b"),
    late(r"\b<OK>\s+от\s+<CLIENT>\b"),
    late(r"\b<CLIENT>\s+(?:одобрил|согласовал|утвердил|принял)\w*"),
]
SHOWN_CLIENT_SOURCE = late(r"\b<SEND>\s+(?:\S+\s+){0,2}?<CLIENT>\b")
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
ROLLBACK_SOURCES = [
    late(r"\b(?:вернул\w*|вернули|вернуло)\s+(?:\S+\s+){0,3}?на\s+правк\w*"),
    late(r"\bправки\s+(?:от|по)\s+<CLIENT>\b"),
    late(
        r"\b<CLIENT>\s+(?:не\s+(?:согласовал|одобрил|принял)\w*|отклонил\w*|забраковал\w*|"
        r"попросил\w*\s+(?:переделать|поправить|исправить))"
    ),
    late(r"\bна доработку\b"),
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
# «Вышел» без уточнений — слишком общее слово («вышла из отпуска»): засчитываем его в коротком
# сообщении, при упоминании поста или когда названа дата, номер или тема.
VAGUE_OUT = rx(r"\b(?:вышел|вышла|вышло)\b")
BARE_OUT = rx(r"^(?:(?:пост|уже|все|всё)\s+)?(?:вышел|вышла|вышло)(?:\s+(?:пост|уже))?[.!\s]*$")
POST_WORD = rx(r"\b(?:пост|мем|текст|подборк|карточк|сторис|опрос|анонс|рилс|клип|тест)\w*")


@dataclass(frozen=True)
class RuleSet:
    """Правила, в которых участвуют имена людей клиента из настроек."""

    stage_rules: list[tuple[Stage, list[re.Pattern[str]]]]
    client_ok: list[re.Pattern[str]]
    shown_client: re.Pattern[str]
    rollback: list[re.Pattern[str]]
    client_stems: tuple[str, ...]


@lru_cache(maxsize=8)
def ruleset(client_names: tuple[str, ...] = ()) -> RuleSet:
    client = client_words(client_names)

    def build(pattern: str) -> re.Pattern[str]:
        return re.compile(pattern.replace("<CLIENT>", client))

    return RuleSet(
        stage_rules=[
            (stage, [build(p) for p in patterns]) for stage, patterns in STAGE_RULE_SOURCES
        ],
        client_ok=[build(p) for p in CLIENT_OK_SOURCES],
        shown_client=build(SHOWN_CLIENT_SOURCE),
        rollback=[build(p) for p in ROLLBACK_SOURCES],
        client_stems=tuple(dict.fromkeys(name_stem(n) for n in client_names if name_stem(n))),
    )


def _anchored(ref: PostRef) -> bool:
    """Пост назван номером, датой или рубрикой — а не просто набором слов."""
    return bool(ref.numbers or ref.has_date or ref.rubrics)


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


def _move(norm: str, today: date, stems: tuple[str, ...]) -> Intent | None:
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
        extract_refs(rest, today, stems),
        new_date=new_date,
        new_weekday=new_weekday,
        rule="move",
    )


def parse_message(text: str, today: date, client_names: tuple[str, ...] = ()) -> Intent | None:
    """Что хотел сказать автор сообщения; None, если сообщение не про посты."""
    if not text or text.lstrip().startswith("/"):
        return None
    norm = normalize(URL.sub(" ", text))
    if not norm or len(norm.split()) > MAX_WORDS:
        return None
    question = "?" in norm
    rules = ruleset(tuple(client_names))

    def refs(source: str) -> PostRef:
        return extract_refs(source, today, rules.client_stems)

    # КП.
    if KP_WORD.search(norm) and not question:
        if KP_OWN_RULE.search(norm):
            return Intent(IntentKind.KP_OWN, PostRef(), rule="kp_own")
        if _first(KP_SHOWN_RULES, norm):
            return Intent(IntentKind.KP_SHOWN, PostRef(), rule="kp_shown")
        if _first(KP_OK_RULES, norm):
            return Intent(IntentKind.KP_OK, PostRef(), rule="kp_ok")

    if NOT_POST_RULE.search(norm):
        return Intent(IntentKind.NOT_POST, refs(norm), rule="not_post")

    if not question:
        move = _move(norm, today, rules.client_stems)
        if move:
            return move

        if _first(rules.rollback, norm) and not FUTURE.search(norm):
            design = re.search(r"\b(?:дизайн\w*|макет\w*|картинк\w*)", norm)
            return Intent(
                IntentKind.ROLLBACK,
                refs(norm),
                stage=Stage.AT_DESIGNER if design else Stage.TAKEN,
                rule="rollback",
            )

    looks_like_post = bool(re.search(r"\b(?:пост|мем|тем)\w*", norm)) or refs(norm).has_date
    wants_release = RELEASE_STRONG.search(norm) or RELEASE_VERBS.search(norm)
    if wants_release and looks_like_post and "дизайнер" not in norm:
        return Intent(IntentKind.RELEASE, refs(norm), rule="release")

    for pattern in ASSIGN_RULES:
        match = pattern.search(norm)
        if match and not question:
            return Intent(
                IntentKind.ASSIGN,
                refs(pattern.sub(" ", norm, count=1)),
                target=match.group("who").lstrip("@"),
                rule="assign",
            )

    if question:
        return None

    if _first(CLAIM_RULES, norm):
        return Intent(IntentKind.CLAIM, refs(norm), rule="claim")

    if FUTURE.search(norm):
        return None

    stages: list[tuple[Stage, str]] = []
    for stage, patterns in rules.stage_rules:
        if _first(patterns, norm):
            stages.append((stage, f"stage:{stage.name.lower()}"))
    if _first([VAGUE_OUT], norm) and (
        BARE_OUT.match(norm) or POST_WORD.search(norm) or _anchored(refs(norm))
    ):
        stages.append((Stage.PUBLISHED, "stage:published"))
    if NOT_SENT.search(norm):
        stages = [(s, r) for s, r in stages if s not in LATE_STAGES]
    if stages:
        stage, rule = max(stages, key=lambda item: item[0])
        return Intent(IntentKind.STAGE, refs(norm), stage=stage, rule=rule)

    if _first(rules.client_ok, norm):
        return Intent(IntentKind.CLIENT_OK, refs(norm), rule="client_ok")
    if _first([rules.shown_client], norm) and not NOT_SENT.search(norm):
        return Intent(IntentKind.SHOWN_CLIENT, refs(norm), rule="shown_client")
    return None
