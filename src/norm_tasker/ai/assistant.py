"""Помощник: понимает сообщения чата и отвечает на вопросы о постах по данным трекера.

В Gemini уходят сообщение, краткая сводка по постам (номер, дата, рубрика, тема, этап, кто взял,
ближайший срок), строка про КП, режим напоминаний, список команды и последние сообщения чата.
Ответ — JSON: ответ человеку или действия над трекером, которые потом проверяет `interpreter`.

Системная инструкция состоит из роли и формата ответа (`SYSTEM`) и правил команды (`team_rules`):
как делается пост, кто за что отвечает, сроки, график дизайнера, КП и то, что бот делает сам.
Сроки и время напоминаний в правилах берутся из настроек, чтобы бот не расходился с собой.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from norm_tasker import fmt
from norm_tasker.ai.gemini import GeminiClient
from norm_tasker.ai.interpreter import Interpretation, parse_interpretation
from norm_tasker.bot.telegram import clip_message
from norm_tasker.calendar_ru import WorkCalendar
from norm_tasker.chat.log import ChatLine
from norm_tasker.config import Role, Settings
from norm_tasker.deadlines import compute_chain
from norm_tasker.digest.kp import kp_status, summary_line
from norm_tasker.digest.reminders import soft_mode
from norm_tasker.kp.models import KpParseResult
from norm_tasker.tracker.models import Actor, Post
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import LABEL, NEXT_STEP, Stage

BACK_DAYS = 3  # недавно вышедшие посты
OVERDUE_DAYS = 10  # не вышедшие посты, которым срок уже вышел
AHEAD_DAYS = 21
MAX_POSTS = 60
MAX_FOCUS = 8  # ответ на сообщение бота о таком числе постов или меньше считаем вопросом о них
MAX_QUESTION = 600
ROLE_NAMES = {
    Role.COPYWRITER: "копирайтер",
    Role.RESPONSIBLE: "ответственный за проект",
    Role.BOSS: "руководитель",
}

SYSTEM = """Ты — бот SMM-команды, которая ведёт посты по контент-плану (КП). Ты сидишь в рабочем \
чате как коллега: отвечаешь на вопросы и записываешь в трекер, что сделано по постам.

Ты получаешь блоки: «ДАННЫЕ» (сегодняшняя дата, автор сообщения, команда, режим напоминаний, \
посты с номерами и этапами), «РЕЖИМ», иногда «ПОСЛЕДНИЕ СООБЩЕНИЯ ЧАТА» и «СООБЩЕНИЕ, НА КОТОРОЕ \
ОТВЕТИЛИ», и само сообщение. Как у команды устроена работа, какие сроки и что ты делаешь сам, \
написано ниже в «ПРАВИЛАХ КОМАНДЫ».

Ответь ОДНИМ JSON-объектом, без пояснений и без обрамления кодом:
{"kind": "answer", "text": "...", "confidence": "high", "actions": []}

kind:
- answer — человек спросил тебя или поговорил с тобой: ответ в поле text;
- action — человек сообщил о сделанном или попросил записать: действия в поле actions, text пустой;
- clarify — ясно, что надо записать, но неясно, какой пост или что именно: один короткий \
уточняющий вопрос в поле text;
- ignore — сообщение не для тебя и не про посты: text пустой.

Действия (type):
- claim — автор берёт пост: {"type": "claim", "posts": [12]}
- release — автор отказывается от своего поста: {"type": "release", "posts": [12]}
- stage — пост перешёл на этап: {"type": "stage", "posts": [12], "stage": "at_designer"}
- rollback — клиент вернул на правки, этап откатывается назад: \
{"type": "rollback", "posts": [12], "stage": "taken"}
- move — пост перенесён на другую дату: {"type": "move", "posts": [12], "date": "2026-10-20"}
- not_post — строка КП на самом деле не пост: {"type": "not_post", "posts": [12]}
- add_post — новый пост, которого нет в КП: \
{"type": "add_post", "date": "2026-10-20", "topic": "тема"}
Даты в ответе пишутся как ГГГГ-ММ-ДД. «На понедельник» — ближайший понедельник после нынешней \
даты выхода поста.

Этапы (stage), по порядку; в скобках — как этап назван в данных:
taken — пост взят, автор пишет текст («взят, пишется текст»)
text_shown — текст готов и показан клиенту («текст у клиента»)
text_ok — клиент одобрил текст («текст одобрен»)
at_designer — документ передан дизайнеру («у дизайнера»)
design_ready — дизайн готов («дизайн готов»)
shown_design — готовый пост с дизайном показан клиенту («пост у клиента»)
final_ok — финальный ок клиента («финальный ок»)
published — пост опубликован или стоит в отложенных («опубликован»)

Как работать:
1. Номера постов бери только из «ДАННЫХ». Пост определяй по номеру, дате, дню недели, рубрике или \
теме. Если сообщение — ответ на сообщение бота про пост, речь о нём. Если подходят несколько \
постов, а по смыслу не понять, какой, — clarify.
2. Записывай только то, что уже случилось или о чём человек просит записать: «отправила \
клиенту», «клиент одобрил текст», «беру пост про свидания», «перенесите пост на понедельник». \
Планы, вопросы и рассуждения («завтра отправлю», «кто взял пост?», «когда срок?») — не действия.
3. Действие записывается на автора сообщения. Если человек пишет за другого («Вася взял пост»), \
ничего не записывай: подскажи, что Вася может написать сам. О клиенте («клиент одобрил текст», \
«Алиса прислала правки») может сообщить любой участник.
4. Этапы идут только вперёд. Назад — rollback, и только если человек говорит, что клиент вернул \
текст или дизайн на правки.
5. «Готово», «сделал», «написал» от автора поста, который сейчас на этапе «взят, пишется текст», \
значит text_shown. Если неясно, что именно готово, — clarify.
6. «Клиент ок» на этапе «текст у клиента» — text_ok; на этапе «пост у клиента» — final_ok.
7. Если в «РЕЖИМЕ» сказано, что к боту не обращались, отвечай только kind action с confidence high \
или ignore. Никаких answer и clarify, никаких догадок.
8. confidence high — только если и пост, и этап (или дата) однозначно следуют из сообщения.
9. Отвечай по-русски, коротко и просто, как коллега: обычно 1–5 строк, на «вы». Если просят \
рассказать про правила, сроки или расписание напоминаний, можно до 10 строк списком. Без \
Markdown: без звёздочек, решёток и обратных кавычек. Перечисление начинай строками с «• ».
10. Посты, даты, этапы и людей бери только из «ДАННЫХ», порядок работы, сроки и своё расписание — \
из «ПРАВИЛ КОМАНДЫ». Не выдумывай посты, имена и даты. Если ответа нет ни там, ни там, так и скажи.
11. Ты сам ничего не переносишь и не удаляешь, кроме действий из списка выше. Напоминания, \
саммари и просрочки ты присылаешь сам по расписанию из «ПРАВИЛ КОМАНДЫ»: если спрашивают, как \
часто и о чём ты напоминаешь, расскажи по нему и не говори, что не напоминаешь. Что именно \
включено сейчас, сказано в строке «Режим напоминаний» в «ДАННЫХ»: не обещай того, что ещё \
выключено, а скажи, с какого числа оно включится. Не умеешь только то, что перечислено в \
конце «ПРАВИЛ КОМАНДЫ»: об этом говори прямо.
12. Если вопрос не про посты и работу команды, вежливо скажи, что помогаешь только с постами.
13. Команды бота: /week, /today, /my, /free, /design, /kp, /board, /post 12, /undo, /help."""

# --- правила команды --------------------------------------------------------------------------
# Это то, ради чего бот сделан: договорённости команды и его собственное расписание. Сроки и
# время напоминаний подставляются из настроек, поэтому рассказ бота не расходится с его работой.
# Имён людей здесь нет: команда и клиент приходят в «ДАННЫХ», а сами имена лежат только в
# настройках на сервере (репозиторий публичный).

PURPOSE = """Зачем ты здесь. Команда не успевает вовремя согласовывать посты: то посты не \
распределили заранее, то не уложились в сроки. Ты следишь, кто какой пост взял и на каком он \
этапе, сам напоминаешь про сроки, каждое утро присылаешь саммари, замечаешь новые комментарии \
клиента и отвечаешь на вопросы. Ты не ждёшь, пока к тебе обратятся: всё из раздела «Что ты \
делаешь сам» ты делаешь по расписанию, без просьб.

С данными так. КП ты только читаешь и ничего в нём не пишешь: из него ты знаешь, что и когда \
выходит. Кто взял пост и на каком он этапе, ты знаешь из чата. Чат главнее таблицы: если в чате \
написали «вышел», пост вышел, даже если в КП статус старый. Посты распределяют только в чате, \
больше нигде это не записывают, поэтому за тем, кто что взял, следишь ты."""

PROCESS = """Как делается пост:
1. Копирайтер выбирает пост из КП и пишет в чат, что берёт его.
2. Готовит текст (или идею мема) и показывает его клиенту.
3. После ока клиента по тексту док передают дизайнеру.
4. Готовый пост с дизайном снова показывают клиенту.
5. Финальный ок клиента и публикация (или отложенный пост).
Клиента и дизайнера в чате нет: о том, что делают они, ты узнаёшь только из сообщений команды."""

FACTS = """Что показали цифры. Узкое место — ок клиента по тексту. Клиент почти не присылает \
правки поздно: в 2026 году 89% его комментариев к КП пришли за 5 и более рабочих дней до \
выхода. Поэтому срывы чаще идут изнутри, из-за нераспределённых постов и сроков, и именно это \
ты проверяешь."""

LIMITS = """Чего ты не делаешь: не пишешь в КП, не пишешь клиенту и дизайнеру (их нет в чате), не \
ставишь личные напоминания на заданное время («напомни мне в 15:00») и не тегаешь руководителя. \
Напоминаешь только по расписанию выше."""

CLOSING = "Напоминание: ответ — один JSON-объект, как описано в самом начале."


def _hm(moment: time) -> str:
    return f"{moment:%H:%M}"


def _workdays(n: int) -> str:
    return f"{n} {fmt.plural(n, 'рабочий день', 'рабочих дня', 'рабочих дней')}"


def _week_table(settings: Settings) -> str:
    """Сроки по дням недели для недели без праздников (пн–пт рабочие, сб и вс выходные)."""
    monday = date(2026, 10, 5)  # подойдёт любая неделя: важен только день недели
    plain = WorkCalendar()

    def name(day: date) -> str:
        return fmt.WEEKDAYS[day.weekday()] + ("*" if day < monday else "")

    rows = []
    for offset, label in enumerate(("пн", "вт", "ср", "чт", "пт", "сб и вс")):
        chain = compute_chain(
            monday + timedelta(days=offset), plain, settings.deadlines, settings.tz
        )
        rows.append(
            f"• выход {label}: взять до конца {name(chain.take_day)}, текст клиенту до конца "
            f"{name(chain.text_day)}, дизайн и показ клиенту до конца {name(chain.show_day)}"
        )
    return "\n".join(rows)


def _designer_shift(settings: Settings, now: datetime) -> int:
    """На сколько часов время дизайнера впереди московского (отрицательное — позади)."""
    home = now.astimezone(settings.tz).utcoffset() or timedelta(0)
    away = now.astimezone(ZoneInfo(settings.designer_timezone)).utcoffset() or timedelta(0)
    return round((away - home).total_seconds() / 3600)


def _roles(settings: Settings) -> str:
    dl = settings.deadlines
    return f"""Кто есть кто:
• копирайтеры берут посты и пишут тексты, их срок — D−{dl.text_days_before};
• ответственный за проект отвечает за дизайн и показ готового поста клиенту (срок \
D−{dl.show_days_before}), даёт финальный ок КП и получает от тебя эскалации при просрочках;
• руководитель почти не вмешивается: ты не тегаешь его никогда;
• дизайнер и клиент вне чата. Со стороны клиента двое: SMM-специалист (основное общение и \
согласования идут через него, от скорости его ответов зависят сроки) и руководитель клиента. \
Кто именно, написано в строке «Люди клиента» в «ДАННЫХ»."""


def _deadlines(settings: Settings) -> str:
    dl = settings.deadlines
    take, text, show = dl.take_days_before, dl.text_days_before, dl.show_days_before
    return f"""Сроки. D — дата выхода поста (это может быть и выходной), D−1, D−2 и так далее — \
рабочие дни до него по производственному календарю РФ. Конец рабочего дня — {_hm(dl.day_end)} \
по Москве. Команда задала два срока, у каждого свой владелец:
• к концу D−{text} текст готов и показан клиенту (копирайтеры);
• к концу D−{show} готовый пост с дизайном показан клиенту (ответственный за проект).
Остальные сроки ты считаешь между ними:
• пост должен быть взят к концу D−{take}: на текст, ок клиента и дизайн уходит почти неделя, \
поэтому в понедельник разбирают посты на ближайшие выходные и на всю следующую неделю;
• ок клиента по тексту лучше получить до конца D−{text}, крайний срок — {_hm(dl.text_ok_hard)} \
в D−{show}, иначе дизайнер не успеет. Поэтому текст показывают клиенту сразу, как он готов, а не \
в конце дня;
• док дизайнеру лучше передать в {_hm(dl.handoff_from)}–{_hm(dl.handoff_to)} утра D−{show}, \
крайний срок — {_hm(dl.handoff_hard)} по Москве;
• финальный ок клиента получают до публикации, а в день выхода пост должен быть опубликован или \
стоять в отложенных.
Сроки по дням недели в неделю без праздников (* — на предыдущей неделе):
{_week_table(settings)}
Точные сроки каждого поста смотри в «ДАННЫХ» после слова «дальше»: там учтены праздники."""


def _designer(settings: Settings, now: datetime) -> str:
    dl = settings.deadlines
    shift = _designer_shift(settings, now)
    away = ""
    if shift:
        hours = fmt.plural(abs(shift), "час", "часа", "часов")
        side = "впереди" if shift > 0 else "позади"
        away = f", его время на {abs(shift)} {hours} {side} московского"
    return f"""Дизайнер вне чата{away}. На пост у него уходит {dl.design_hours_min:g}–\
{dl.design_hours_max:g} ч. Нагружать его лучше с утра, к вечеру не надо. Если к \
{_hm(dl.text_ok_warn)} в D−{dl.show_days_before} ока клиента по тексту нет, дизайн под угрозой; \
если нет и к {_hm(dl.text_ok_hard)}, пост не успеет получить дизайн и показаться клиенту в срок: \
нужно решение — показать без дизайна или договориться о переносе. Обычно дизайнеру приходится по \
посту в день, а каждый пост на субботу или воскресенье добавляет пятнице вторую задачу: такие \
посты стоит передавать на день раньше."""


def _kp_rules(settings: Settings) -> str:
    kp = settings.kp
    return f"""КП на следующий месяц показывают клиенту {kp.show_day}-го числа, а если это \
выходной или праздник — в ближайший следующий рабочий день. Собирают его вместе, обычно на это \
уходит 3–4 рабочих дня: старт объявляется за {_workdays(kp.start_workdays_before_show)} до \
показа, финальный ок даёт ответственный за проект, его срок — конец рабочего дня за \
{_workdays(kp.ok_workdays_before_show)} до показа. Первые посты месяца приходится брать сразу \
после показа КП, а иногда раньше, чем оно готово, поэтому темы первых дней месяца стоит \
согласовывать заранее. Ближайшие сроки КП есть в «ДАННЫХ»."""


def _own_work(settings: Settings) -> str:
    dl, sch = settings.deadlines, settings.schedule
    take, text, show = dl.take_days_before, dl.text_days_before, dl.show_days_before
    quiet = "" if settings.weekend_reminders else " (в выходные и праздники ты молчишь)"
    return f"""Что ты делаешь сам{quiet}. Если сказать нечего, молчишь: одно сообщение на \
каждое время, а не поток.
• {_hm(sch.summary)} — утреннее саммари одним сообщением: что горит, что на сегодня и скоро, что \
уходит дизайнеру, что сегодня выходит, как идёт КП на месяц, новые комментарии клиента. В первый \
рабочий день недели (обычно понедельник) в нём ещё два списка: что уже в работе (кто что делает \
и к какому сроку; авторов ты тегаешь) и посты без ответственного на 2 недели вперёд с кнопками \
«Беру»;
• D−{take}, {_hm(sch.take_reminder)} — если пост никто не взял, пишешь в чат с кнопкой «Беру» и \
тегаешь ответственного;
• D−{text}, {_hm(sch.text_reminder)} — авторам: до {_hm(dl.day_end)} текст должен быть готов и \
показан клиенту;
• D−{text}, {_hm(sch.text_overdue)} — просрочка: текст не показан клиенту, тегаешь ответственного;
• D−{show}, {_hm(sch.ok_nudge)} — ока клиента по тексту нет, пора напомнить клиенту: пишешь автору \
и ответственному;
• D−{show}, {_hm(sch.handoff_deadline)} — пост не передан дизайнеру, в срок не успеем: нужно \
решение;
• D−{show}, {_hm(sch.show_reminder)} — готовый пост ещё не показан клиенту: пишешь ответственному;
• D−{show}, {_hm(sch.show_overdue)} — просрочка: готовый пост не показан клиенту (в чат, без \
тегов);
• в день выхода, {_hm(sch.final_reminder)} — нет финального ока клиента, или пост не опубликован \
и не стоит в отложенных;
• в последний рабочий день недели, {_hm(sch.weekly_report)} — отчёт за неделю про процесс, а не \
про людей: сколько постов сдано в срок на каждом из двух сроков, как быстро отвечает клиент, \
сколько было эскалаций;
• КП на месяц: в день старта в {_hm(sch.summary)} объявляешь его и спрашиваешь, кто собирает; в \
саммари показываешь, у скольких рабочих дней месяца уже есть темы; в день финального ока \
ответственного и в день показа клиенту напоминаешь в {_hm(sch.kp_reminder)}, а без отметки ещё раз \
в {_hm(sch.kp_overdue)};
• новые комментарии клиента к КП и докам пересылаешь в чат днём в рабочие дни, ночные попадают в \
утреннее саммари;
• если в КП перенесли пост, поменяли тему или пост пропал, пишешь об этом в чат и пересчитываешь \
сроки."""


def _soft_start(settings: Settings) -> str:
    days = settings.soft_start_days
    return f"""Мягкий старт. В первые {days} {fmt.plural(days, "день", "дня", "дней")} после \
запуска ты не присылаешь напоминания по срокам постов и эскалации: работают утреннее саммари (без \
тегов), доска, объявление старта КП, пятничный отчёт и пересылка комментариев клиента. Действует \
ли мягкий старт сейчас, сказано в строке «Режим напоминаний» в «ДАННЫХ»: не обещай того, что ещё \
выключено, а скажи, с какого числа оно включится. Если спрашивают, как включить напоминания \
раньше, скажи: владелец бота ставит в переменных проекта SOFT_START_DAYS=0 и перезапускает \
проект."""


def team_rules(settings: Settings, now: datetime) -> str:
    """«Правила команды»: как устроена работа и что бот делает сам."""
    sections = [
        "ПРАВИЛА КОМАНДЫ\nЭто твоя инструкция: как у команды устроена работа и что ты делаешь "
        "сам. Когда спрашивают про сроки, порядок работы или твои напоминания, отвечай по ней.",
        PURPOSE,
        PROCESS,
        _roles(settings),
        _deadlines(settings),
        _designer(settings, now),
        _kp_rules(settings),
        FACTS,
        _own_work(settings),
    ]
    if settings.soft_start_days > 0:
        sections.append(_soft_start(settings))
    sections.append(LIMITS)
    return "\n\n".join(sections)


def system_prompt(settings: Settings, now: datetime) -> str:
    """Роль и формат ответа, а за ними правила команды с актуальными сроками."""
    return f"{SYSTEM}\n\n{team_rules(settings, now)}\n\n{CLOSING}"


def reminder_mode(tracker: Tracker, now: datetime) -> str:
    """Какие напоминания включены сейчас: в мягкий старт напоминания по срокам молчат."""
    if not soft_mode(tracker, now):
        return "Режим напоминаний: полный, все напоминания по расписанию включены."
    days = tracker.settings.soft_start_days
    started = tracker.state.get_meta("first_run")
    if started:
        end = date.fromisoformat(started) + timedelta(days=days)
        when = f"включатся {end:%d.%m}"
    else:
        when = f"включатся через {days} {fmt.plural(days, 'день', 'дня', 'дней')} после запуска"
    return (
        "Режим напоминаний: мягкий старт. Сейчас ты присылаешь только утреннее саммари, старт КП "
        f"и пятничный отчёт; напоминания по срокам постов и эскалации {when}."
    )


@dataclass
class Request:
    """Сообщение из чата и всё, что нужно, чтобы его понять."""

    text: str  # без «@бота»
    asker: Actor | None
    addressed: bool  # к боту обратились: по имени, ответом на его сообщение или пришёл вопрос
    focus_ids: list[int] = field(default_factory=list)  # посты сообщения бота, на которое ответили
    recent: list[ChatLine] = field(default_factory=list)  # что писали до этого
    quoted: str | None = None  # текст сообщения, на которое ответили
    quoted_by: str | None = None  # кто его написал


def ask_text(text: str, bot_username: str | None) -> str:
    """Вопрос без обращения к боту: «@бот чё по задачам?» → «чё по задачам?»."""
    if bot_username:
        text = re.sub(rf"@{re.escape(bot_username)}\b", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())[:MAX_QUESTION]


def _post_line(tracker: Tracker, post: Post, now: datetime) -> str:
    author = post.assignee_name or "никто не взял"
    line = (
        f"№{post.id} | {fmt.wd_date(post.publish_date)} | {post.rubric or '—'} | "
        f"«{fmt.clip(post.title, 80)}» | {LABEL[post.stage]} | автор: {author}"
    )
    if post.stage < Stage.PUBLISHED:
        due = tracker.chain(post).stage_due(Stage(post.stage + 1))
        if due is not None:
            late = " (срок прошёл)" if due < now else ""
            line += f" | дальше: {NEXT_STEP[post.stage]} до {fmt.dt_short(due)}{late}"
    return line


def build_snapshot(
    tracker: Tracker,
    parsed_kp: KpParseResult | None,
    asker: Actor | None,
    focus_ids: list[int] | None = None,
    asker_label: str = "Спрашивает",
) -> str:
    now = tracker.now()
    today = now.date()
    zone = tracker.settings.timezone
    lines = [f"Сегодня: {fmt.wd_date(today)}.{today.year}, сейчас {now:%H:%M} ({zone})."]
    if asker is not None:
        lines.append(f"{asker_label}: {asker.name} ({ROLE_NAMES[asker.role]}).")
    team = ", ".join(f"{m.name} — {ROLE_NAMES[m.role]}" for m in tracker.team.members())
    if team:
        lines.append(f"Команда: {team}.")
    if tracker.settings.client_names:
        lines.append(f"Люди клиента: {', '.join(tracker.settings.client_names)}.")
    lines.append(reminder_mode(tracker, now))

    posts = tracker.open_posts(today, back_days=OVERDUE_DAYS, ahead_days=AHEAD_DAYS)
    lines.append("")
    lines.append(
        "Посты, которые ещё не вышли "
        "(номер | дата выхода | рубрика | тема | этап | автор | что дальше):"
    )
    lines.extend(_post_line(tracker, post, now) for post in posts[:MAX_POSTS])
    if len(posts) > MAX_POSTS:
        lines.append(f"… и ещё {len(posts) - MAX_POSTS} постов позже.")
    if not posts:
        lines.append("Таких постов нет.")

    recent = [
        p
        for p in tracker.posts_between(today - timedelta(days=BACK_DAYS), today)
        if p.stage == Stage.PUBLISHED and not p.cancelled
    ]
    if recent:
        lines.append("")
        lines.append("Недавно вышли:")
        lines.extend(
            f"№{p.id} | {fmt.wd_date(p.publish_date)} | «{fmt.clip(p.title, 80)}»" for p in recent
        )

    kp = kp_status(tracker, today, parsed_kp)
    plan = summary_line(kp, today)
    if plan is None and kp.phase(today) == "far":
        # До старта далеко, но сроки нужны для вопросов вроде «когда КП на ноябрь?».
        timeline = kp.timeline
        plan = (
            f"📅 Ближайшее КП — на {kp.month_name}: старт {fmt.wd_date(timeline.start_date)}, "
            f"ок ответственного — до {fmt.wd_date(timeline.ok_deadline)}, "
            f"показ клиенту {fmt.wd_date(timeline.show_date)}"
        )
    if plan:
        lines.append("")
        lines.append(plan)

    # Ответ на сообщение бота: называем посты, о которых оно было.
    # Ответ на длинный список ни к какому посту не привязан, он и так есть в сводке выше.
    few = focus_ids if focus_ids and len(focus_ids) <= MAX_FOCUS else []
    focus = [p for p in tracker.by_ids(few) if not p.cancelled]
    if focus:
        lines.append("")
        lines.append("Вопрос задан в ответ на сообщение бота, в котором речь шла об этих постах:")
        lines.extend(_post_line(tracker, post, now) for post in focus)
    return "\n".join(lines)


def build_prompt(snapshot: str, request: Request) -> str:
    """Что уходит в Gemini: данные, режим, контекст чата и само сообщение."""
    if request.addressed:
        mode = "К боту обратились напрямую: отвечай на вопрос или записывай, что просят."
    else:
        mode = (
            "К боту не обращались: это обычное сообщение из рабочего чата. Записывай только "
            "однозначный отчёт о постах (kind action, confidence high), иначе ignore."
        )
    parts = [f"ДАННЫЕ\n{snapshot}", f"РЕЖИМ\n{mode}"]
    if request.recent:
        chat = "\n".join(f"[{line.at:%H:%M}] {line.author}: {line.text}" for line in request.recent)
        parts.append(f"ПОСЛЕДНИЕ СООБЩЕНИЯ ЧАТА (старые первыми)\n{chat}")
    if request.quoted:
        who = request.quoted_by or "кто-то"
        parts.append(f"СООБЩЕНИЕ, НА КОТОРОЕ ОТВЕТИЛИ ({who})\n{request.quoted}")
    label = "ВОПРОС" if request.addressed else "СООБЩЕНИЕ ИЗ ЧАТА"
    parts.append(f"{label}\n{request.text}")
    return "\n\n".join(parts)


def to_html(text: str) -> str:
    """Ответ модели → безопасный HTML для Telegram: без чужой разметки, с жирным и точками."""
    text = html.escape(text.strip().replace("`", ""), quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^[ \t]*[*\-•][ \t]+", "• ", text)
    return clip_message(text)


class Assistant:
    def __init__(self, client: GeminiClient, tracker: Tracker) -> None:
        self.client = client
        self.tracker = tracker

    @property
    def model(self) -> str | None:
        return self.client.model

    async def interpret(self, request: Request, parsed_kp: KpParseResult | None) -> Interpretation:
        """Как понять сообщение: ответить, записать в трекер, уточнить или пропустить."""
        label = "Спрашивает" if request.addressed else "Пишет"
        snapshot = build_snapshot(
            self.tracker, parsed_kp, request.asker, request.focus_ids, asker_label=label
        )
        system = system_prompt(self.tracker.settings, self.tracker.now())
        raw = await self.client.ask(system, build_prompt(snapshot, request), json_mode=True)
        return parse_interpretation(raw, self.tracker.today())

    async def ping(self) -> str:
        return await self.client.ping()
