"""Ответы на команды бота. Чистые функции: Telegram здесь не нужен."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

from norm_tasker import __version__, fmt
from norm_tasker.chat.actions import added_post_reply
from norm_tasker.chat.refs import extract_refs, find_dates, topic_score
from norm_tasker.config import Role
from norm_tasker.digest.board import build_board
from norm_tasker.digest.issues import post_issue
from norm_tasker.digest.kp import kp_status
from norm_tasker.digest.reminders import soft_mode
from norm_tasker.digest.summary import build_summary, free_posts_block
from norm_tasker.kp.models import KpParseResult
from norm_tasker.reply import Button, Reply
from norm_tasker.tracker.models import Actor, Post
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import LABEL, NEXT_STEP, Stage, normalize
from norm_tasker.tracker.sync import comments_of

HELP = """Я слежу за постами из КП и за тем, кто что делает. Пишите в чате как обычно:

• «беру пост на пятницу», «возьму №12» — записать, что пост ваш;
• «текст готов, отправил клиенту», «клиент ок по тексту», «отдал дизайнеру», \
«дизайн готов», «показал клиенту с дизайном», «финальный ок», «вышел» — сдвинуть этап;
• «не успеваю пост на среду» — отдать пост;
• «пост №12 переносим на 20.10» — перенести;
• «кп окей», «показали кп клиенту», «беру кп» — про КП на следующий месяц;
• ответ «не пост» на моё сообщение — убрать строку из трекера;
{ask}

Если не уверен, спрошу кнопками. Любое действие можно отменить кнопкой или командой /undo.

Команды:
/today — что горит сегодня
/week — посты на неделю
/my — мои посты
/free — свободные посты
/design — что уходит дизайнеру
/kp — КП на следующий месяц
/post 16.10 или /post 12 — всё по посту
/add 15.10 тема — добавить пост, которого нет в КП
/board — доска (закрепить в чате)
/undo — отменить моё последнее действие
/status — как я работаю
/id — идентификаторы чата и ваш"""


ASK_PLAIN = (
    "• позовите меня по имени или ответьте на моё сообщение: «что по задачам», «что горит», "
    "«мои посты», «свободные посты» — отвечу так же, как на команды ниже."
)
ASK_AI = (
    "• говорите со мной как с коллегой: позовите по имени («бот, …»), через @ или ответьте на "
    "моё сообщение — «отдай пост про … дизайнеру», «когда срок у моего поста?», «что у нас на "
    "неделе?». Отвечу и запишу, что нужно. Понимает меня ИИ (Gemini, Google): сообщения про "
    "посты уходят туда на разбор, даже если ко мне не обращались."
)

AI_NOTICE = (
    "🧠 Теперь я понимаю обычные фразы с помощью ИИ (Gemini, Google): сообщения чата про посты "
    "уходят туда на разбор. Пишите мне как коллеге: «бот, отдай пост про … дизайнеру», "
    "«что у меня горит?». Если такое чтение чата не нужно, владелец бота может его выключить."
)
NOT_ALLOWED = "Записывать в трекер могут копирайтеры и ответственный за проект."
NOT_RECORDED = (
    "Не получилось это записать: не нашёл такой пост или ничего не изменилось. "
    "Посты недели — /week."
)

UNSURE = """Не понял, что нужно. Обычные вопросы я пока не разбираю — только команды \
и короткие отчёты о постах («беру пост на пятницу», «текст готов»).

/week — посты на неделю
/today — что горит сегодня
/my — мои посты
/free — свободные посты
/board — доска
/help — всё, что я умею"""


@dataclass
class RuntimeInfo:
    """Что бот знает о собственной работе: для /status."""

    started_at: datetime | None = None
    last_kp_sync: datetime | None = None
    last_kp_error: str | None = None
    last_tracker_copy: datetime | None = None
    last_tracker_copy_error: str | None = None
    kp_posts: int = 0
    warnings: list[str] = field(default_factory=list)
    google_configured: bool = False
    tracker_copy_configured: bool = False
    ai_configured: bool = False  # есть ключ Gemini и ИИ не выключен
    ai_model: str | None = None
    ai_ok_at: datetime | None = None  # когда Gemini отвечал в последний раз
    ai_error: str | None = None  # чем кончилась последняя попытка, если неудачей


def help_reply(ai: bool = False) -> Reply:
    return Reply(HELP.replace("{ask}", ASK_AI if ai else ASK_PLAIN), kind="help")


def unsure_reply() -> Reply:
    return Reply(UNSURE, kind="help")


def my_view(tracker: Tracker, actor: Actor) -> Reply:
    today = tracker.today()
    posts = [p for p in tracker.open_posts(today, back_days=3) if p.owned_by(actor)]
    if not posts:
        return Reply("За вами ничего нет. Свободные посты — /free.")
    lines = []
    for post in posts:
        chain = tracker.chain(post)
        due = chain.stage_due(Stage(post.stage + 1)) if post.stage < Stage.PUBLISHED else None
        step = f"; {NEXT_STEP[post.stage]} — до {fmt.dt_short(due)}" if due else ""
        lines.append(f"• {fmt.line(post)} — {fmt.stage_text(post.stage)}{step}")
    return Reply("Ваши посты:\n" + "\n".join(lines), post_ids=[p.id for p in posts])


def free_view(tracker: Tracker) -> Reply:
    block = free_posts_block(tracker, tracker.today())
    if block is None:
        return Reply("Свободных постов на две недели вперёд нет 🎉")
    text, buttons, post_ids = block
    return Reply(text, buttons, post_ids=post_ids, kind="free")


def design_view(tracker: Tracker) -> Reply:
    today = tracker.today()
    tomorrow = tracker.calendar.next_workday(today)
    posts = tracker.open_posts(today, back_days=1)
    lines = []
    for label, day in (("Сегодня", today), (f"Дальше, {fmt.wd_date(tomorrow)}", tomorrow)):
        rows = []
        for post in posts:
            chain = tracker.chain(post)
            if chain.show_day != day or not Stage.TAKEN <= post.stage <= Stage.DESIGN_READY:
                continue
            note = {
                Stage.TAKEN: "текст ещё пишется",
                Stage.TEXT_SHOWN: "ждём ок по тексту",
                Stage.TEXT_OK: (
                    f"передать до {chain.handoff_ideal:%H:%M} "
                    f"(крайний срок {chain.handoff_hard:%H:%M})"
                ),
                Stage.AT_DESIGNER: "у дизайнера",
                Stage.DESIGN_READY: "дизайн готов, показать клиенту",
            }[post.stage]
            rows.append(f"• {fmt.label(post, 45)} — {note}")
        if rows:
            lines.append(f"{label}:\n" + "\n".join(rows))
    if not lines:
        return Reply("🎨 Дизайнеру на сегодня и завтра ничего не запланировано.")
    return Reply("🎨 Дизайн\n\n" + "\n\n".join(lines))


def kp_view(tracker: Tracker, parsed: KpParseResult | None) -> Reply:
    today = tracker.today()
    status = kp_status(tracker, today, parsed)
    timeline = status.timeline
    lines = [
        f"📅 КП на {status.month_name}",
        f"Старт сборки: {fmt.wd_date(timeline.start_date)}",
        f"Финальный ок ответственного — до {fmt.wd_date(timeline.ok_deadline)}"
        + (" ✅ дан" if status.ok_at else ""),
        f"Показ клиенту: {fmt.wd_date(timeline.show_date)}"
        + (" ✅ показано" if status.shown_at else ""),
    ]
    if status.owner_name:
        lines.append(f"Собирает: {escape(status.owner_name)}")
    if parsed is None:
        lines.append("\nКП ещё ни разу не прочитано — прогресс появится после первой сверки.")
    elif not status.sheet_exists:
        lines.append(
            f"\nЛист «{status.month_name.capitalize()} {timeline.month.year}» ещё не создан."
        )
    else:
        lines.append(f"\nТемы есть у {status.filled} из {status.total} рабочих дней.")
        with_topic = {s.date for s in parsed.slots if s.slot == 1 and s.topic}
        first = timeline.month
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        day = first - timedelta(days=first.weekday())
        while day <= last:
            week = [day + timedelta(days=i) for i in range(5)]
            inside = [d for d in week if d.month == timeline.month.month]
            if inside:
                have = sum(d in with_topic for d in inside)
                lines.append(f"• {day:%d.%m}–{week[-1]:%d.%m}: {have} из {len(inside)}")
            day += timedelta(days=7)
    return Reply("\n".join(lines))


def find_posts(tracker: Tracker, query: str) -> list[Post]:
    today = tracker.today()
    ref = extract_refs(query, today)
    if ref.numbers:
        return [p for p in tracker.by_ids(list(ref.numbers)) if not p.cancelled]
    bare = normalize(query).strip()
    if bare.isdigit():
        found = tracker.get(int(bare))
        return [found] if found and not found.cancelled else []
    window = tracker.posts_between(today - timedelta(days=30), today + timedelta(days=60))
    matches = [
        p
        for p in window
        if p.publish_date in ref.dates
        or p.publish_date.weekday() in ref.weekdays
        or p.publish_date.day in ref.day_numbers
    ]
    if ref.weekdays and not ref.dates:
        upcoming = [p for p in matches if p.publish_date >= today - timedelta(days=1)]
        if upcoming:
            nearest = min(p.publish_date for p in upcoming)
            matches = [p for p in upcoming if p.publish_date == nearest]
    if not matches and ref.words:
        scored = [(topic_score(ref.words, p.topic), p) for p in window]
        best = max((s for s, _ in scored), default=0)
        matches = [p for s, p in scored if best > 0 and s == best]
    return matches[:3]


def post_view(tracker: Tracker, post: Post) -> str:
    now = tracker.now()
    chain = tracker.chain(post)
    lines = [
        f"№{post.id} «{escape(post.title)}»",
        f"Выход: {fmt.wd_date(post.publish_date)}"
        + (
            f" (в КП: {fmt.wd_date(post.kp_date)})"
            if post.kp_date and post.kp_date != post.publish_date
            else ""
        )
        + (f" · {escape(post.rubric)}" if post.rubric else ""),
        f"Автор: {fmt.author(post)}",
        f"Этап: {fmt.stage_text(post.stage)}",
    ]
    if post.kp_status:
        lines.append(f"Статус в КП: {escape(post.kp_status)}")
    if not post.in_kp:
        lines.append("⚠️ Поста нет в КП")
    if post.doc_url:
        lines.append(f'<a href="{escape(post.doc_url, quote=True)}">Док</a>')
    issue = post_issue(tracker, post, now)
    if issue:
        lines.append(f"{'🔴' if issue.level == 'red' else '🟡'} {issue.text}")
    lines += [
        "",
        "Сроки:",
        f"• взять — до {fmt.dt_short(chain.take_by)}",
        f"• текст готов и показан клиенту — до {fmt.dt_short(chain.text_shown_by)}",
        f"• ок по тексту — до {fmt.dt_short(chain.text_ok_hard)}",
        f"• передать дизайнеру — до {fmt.dt_short(chain.handoff_hard)}",
        f"• показать клиенту с дизайном — до {fmt.dt_short(chain.show_by)}",
        f"• выход — {fmt.wd_date(post.publish_date)}",
    ]
    comments = comments_of(tracker, post.id)
    if comments:
        lines += ["", "Открытые комментарии клиента:"]
        lines += [f"• {escape(c.author)}: «{escape(fmt.clip(c.text, 150))}»" for c in comments[-3:]]
    history = [e for e in tracker.history(post.id, limit=6) if not e.kind.startswith("kp_sync")]
    if history:
        lines += ["", "Последние события:"]
        for entry in history[:5]:
            who = escape(entry.actor_name) if entry.actor_name else "КП"
            stage = f" → {LABEL[Stage(entry.after['stage'])]}" if "stage" in entry.after else ""
            lines.append(f"• {entry.ts:%d.%m %H:%M} {who}: {entry.kind}{stage}")
    return "\n".join(lines)


def week_view(tracker: Tracker, parsed: KpParseResult | None) -> Reply:
    return Reply(build_board(tracker, tracker.now(), parsed))


def today_view(tracker: Tracker, parsed: KpParseResult | None) -> Reply:
    reply, _ = build_summary(tracker, tracker.now(), parsed, soft=False)
    return Reply(reply.text, reply.buttons, post_ids=reply.post_ids, kind=reply.kind)


def add_post_from_text(tracker: Tracker, actor: Actor, args: str) -> Reply:
    """/add 15.10 тема поста"""
    today = tracker.today()
    dates = find_dates(normalize(args), today)
    if not dates:
        return Reply("Укажите дату, например: /add 15.10 срочный пост про акцию")
    start, end, day = dates[0]
    topic = " ".join((args[:start] + " " + args[end:]).split()).strip(" ,:—-") or None
    if actor.role == Role.BOSS:
        return Reply()
    return added_post_reply(tracker, actor, tracker.add_post(day, topic, actor))


def inventory_view(tracker: Tracker) -> Reply:
    """Первая инвентаризация: показать посты на две недели и попросить разметить."""
    today = tracker.today()
    posts = [
        p
        for p in tracker.posts_between(today - timedelta(days=1), today + timedelta(days=14))
        if p.stage < Stage.PUBLISHED
    ]
    if not posts:
        return Reply("В трекере нет постов на ближайшие две недели. Проверьте КП: /kp и /status.")
    lines = [
        f"• {fmt.line(p)} — {fmt.stage_text(p.stage)}"
        + (f" ({escape(p.assignee_name)})" if p.assignee_name else "")
        for p in posts
    ]
    free = [p for p in posts if not p.assigned][:8]
    buttons = [
        [
            Button(f"Беру №{p.id} · {fmt.wd_date(p.publish_date)}", f"claim:{p.id}")
            for p in free[i : i + 2]
        ]
        for i in range(0, len(free), 2)
    ]
    text = (
        "🗂 Давайте сверим трекер с реальностью: посты на две недели вперёд.\n"
        "Кто что делает — напишите в чате как обычно, например: «беру №12», "
        "«текст на пятницу уже у клиента», «по №14 клиент ок, отдал дизайнеру». "
        "Свободные посты можно взять кнопкой.\n\n" + "\n".join(lines)
    )
    return Reply(text[:4000], buttons, post_ids=[p.id for p in posts], kind="free")


def status_view(
    tracker: Tracker, info: RuntimeInfo, chat_ok: bool, ai_listen: bool = False
) -> Reply:
    now = tracker.now()
    lines = [f"🤖 Версия {__version__}"]
    if info.started_at:
        lines.append(f"Работаю с {info.started_at:%d.%m %H:%M}")
    lines.append(f"Чат настроен: {'да' if chat_ok else 'нет — укажите chat_id в config.yaml'}")
    if info.google_configured:
        if info.last_kp_sync:
            lines.append(
                f"КП прочитано {info.last_kp_sync:%d.%m %H:%M}, постов из КП: {info.kp_posts}"
            )
        else:
            lines.append("КП ещё не читалось")
        if info.last_kp_error:
            lines.append(f"⚠️ Последняя ошибка чтения КП: {escape(info.last_kp_error)}")
    else:
        lines.append("⚠️ Google не подключён: КП не читается")
    if info.tracker_copy_configured:
        stamp = (
            f"{info.last_tracker_copy:%d.%m %H:%M}"
            if info.last_tracker_copy
            else "ещё не записывалась"
        )
        lines.append(f"Копия трекера в Google-таблице: {stamp}")
        if info.last_tracker_copy_error:
            lines.append(f"⚠️ Ошибка записи копии: {escape(info.last_tracker_copy_error)}")
    if info.google_configured and not info.tracker_copy_configured:
        lines.append("Без ключа Google: нет комментариев клиента к КП и докам и копии трекера")
    if soft_mode(tracker, now):
        started = tracker.state.get_meta("first_run")
        lines.append(f"Мягкий старт: только саммари и доска (с {started or 'первого запуска'})")
    if info.ai_configured:
        model = f", модель {escape(info.ai_model)}" if info.ai_model else ""
        lines.append(
            f"🧠 ИИ: Gemini{model}. Отвечаю, когда меня зовут или отвечают на моё сообщение"
        )
        if ai_listen:
            lines.append(
                "Читаю сообщения чата про посты и записываю отчёты сам (AI_LISTEN=0 выключит)"
            )
        else:
            lines.append("Сообщения, обращённые не ко мне, не читаю")
        if info.ai_error:
            lines.append(f"⚠️ ИИ не отвечает: {escape(info.ai_error)}")
        elif info.ai_ok_at:
            lines.append(f"ИИ отвечал {info.ai_ok_at:%d.%m %H:%M}")
    else:
        lines.append("ИИ не подключён: понимаю кнопки и типовые фразы")
    lines.extend(f"⚠️ {escape(w)}" for w in info.warnings[:5])
    return Reply("\n".join(lines))


def id_text(
    chat_id: int,
    thread_id: int | None,
    user_id: int | None,
    username: str | None,
    actor: Actor | None,
) -> str:
    who = f"@{username}" if username else "без username"
    role = {
        Role.COPYWRITER: "копирайтер",
        Role.RESPONSIBLE: "ответственный за проект",
        Role.BOSS: "руководитель",
    }
    lines = [
        f"chat_id: <code>{chat_id}</code>",
        f"thread_id: <code>{thread_id}</code>" if thread_id else "тема: нет (это не тема группы)",
        f"Ваш id: <code>{user_id}</code> ({who})",
    ]
    if actor:
        lines.append(f"В настройках вы: {escape(actor.name)}, {role[actor.role]}")
    else:
        lines.append("Вас нет в настройках команды")
    return "\n".join(lines)
