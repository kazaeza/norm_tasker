"""Напоминания по расписанию из плана.

На каждое время — одно сообщение со всеми подходящими постами; если всё в порядке, бот молчит.
Просрочки на этапах ответственного за проект пишутся в общий чат без тегов, руководителя бот
не тегает никогда. Правило срабатывает раз в сутки: если бот был выключен в назначенное время,
он догонит пропущенное в течение получаса–полутора часов (настройка), а не завтра.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape

from norm_tasker import fmt
from norm_tasker.digest.kp import kp_status
from norm_tasker.digest.report import build_weekly_report
from norm_tasker.digest.summary import build_summary, last_workday_of_week
from norm_tasker.kp.models import KpParseResult
from norm_tasker.reply import Button, Reply, claim_button, stage_button
from norm_tasker.tracker.models import Post
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import Stage

log = logging.getLogger(__name__)
MAX_BUTTONS = 6


@dataclass
class Ctx:
    tracker: Tracker
    now: datetime
    parsed_kp: KpParseResult | None
    soft: bool
    posts: list[Post] = field(default_factory=list)

    @property
    def today(self) -> date:
        return self.now.date()

    def where(self, predicate: Callable[[Post], bool]) -> list[Post]:
        return [p for p in self.posts if predicate(p)]

    def chain(self, post: Post):
        return self.tracker.chain(post)

    def tag_responsible(self) -> str:
        return self.tracker.team.mention_responsible()

    def mention_author(self, post: Post) -> str:
        return self.tracker.team.mention_assignee(post)


def _lines(posts: list[Post], *, authors: bool = False, ctx: Ctx | None = None) -> str:
    lines = []
    for post in posts:
        who = ""
        if authors and ctx is not None and post.assigned:
            who = f" — {ctx.mention_author(post)}"
        elif post.assignee_name:
            who = f" — {escape(post.assignee_name)}"
        lines.append(f"• {fmt.line(post)}{who}")
    return "\n".join(lines)


def _buttons(
    posts: list[Post], make: Callable[[Post], Button], per_row: int = 1
) -> list[list[Button]]:
    picked = [make(p) for p in posts[:MAX_BUTTONS]]
    return [picked[i : i + per_row] for i in range(0, len(picked), per_row)]


def _tag(ctx: Ctx) -> str:
    return f"\n{ctx.tag_responsible()}" if ctx.tag_responsible() else ""


# --- правила по постам ----------------------------------------------------------------------


def _summary(ctx: Ctx) -> Reply | None:
    reply, comment_ids = build_summary(ctx.tracker, ctx.now, ctx.parsed_kp, soft=ctx.soft)
    reply.meta["comment_ids"] = comment_ids
    return reply


def _take(ctx: Ctx) -> Reply | None:
    posts = ctx.where(lambda p: ctx.chain(p).take_day == ctx.today and not p.assigned)
    if not posts:
        return None
    return Reply(
        "⏰ Сегодня последний день, чтобы взять пост — иначе текст не успеет к сроку:\n"
        + _lines(posts)
        + _tag(ctx),
        _buttons(posts, lambda p: claim_button(p.id), per_row=2),
        post_ids=[p.id for p in posts],
        kind="free",
    )


def _text_noon(ctx: Ctx) -> Reply | None:
    posts = ctx.where(
        lambda p: ctx.chain(p).text_day == ctx.today and p.assigned and p.stage < Stage.TEXT_SHOWN
    )
    if not posts:
        return None
    chain = ctx.chain(posts[0])
    return Reply(
        f"✍️ Сегодня до {chain.text_shown_by:%H:%M} текст должен быть готов и показан клиенту. "
        "Если уже показали — нажмите кнопку:\n" + _lines(posts, authors=True, ctx=ctx),
        _buttons(
            posts, lambda p: stage_button(p.id, Stage.TEXT_SHOWN, f"✓ №{p.id} показан клиенту")
        ),
        post_ids=[p.id for p in posts],
        kind="remind",
    )


def _text_overdue(ctx: Ctx) -> Reply | None:
    posts = ctx.where(lambda p: ctx.chain(p).text_day == ctx.today and p.stage < Stage.TEXT_SHOWN)
    if not posts:
        return None
    return Reply(
        "🔴 Просрочка: текст не показан клиенту.\n" + _lines(posts) + _tag(ctx),
        _buttons(
            posts, lambda p: stage_button(p.id, Stage.TEXT_SHOWN, f"✓ №{p.id} показан клиенту")
        ),
        post_ids=[p.id for p in posts],
        kind="remind",
    )


def _ok_nudge(ctx: Ctx) -> Reply | None:
    posts = ctx.where(lambda p: ctx.chain(p).show_day == ctx.today and p.stage == Stage.TEXT_SHOWN)
    if not posts:
        return None
    chain = ctx.chain(posts[0])
    people = " ".join(dict.fromkeys(m for p in posts if (m := ctx.mention_author(p))))
    head = (
        "⏳ Ока по тексту нет — пора напомнить клиенту. "
        f"Дизайнеру нужно передать до {chain.handoff_hard:%H:%M}."
    )
    tail = " ".join(x for x in (people, ctx.tag_responsible()) if x)
    return Reply(
        f"{head}\n{_lines(posts)}" + (f"\n{tail}" if tail else ""),
        _buttons(posts, lambda p: stage_button(p.id, Stage.TEXT_OK, f"✓ №{p.id} ок получен")),
        post_ids=[p.id for p in posts],
        kind="remind",
    )


def _handoff_hard(ctx: Ctx) -> Reply | None:
    posts = ctx.where(
        lambda p: ctx.chain(p).show_day == ctx.today and Stage.TAKEN <= p.stage < Stage.AT_DESIGNER
    )
    if not posts:
        return None
    return Reply(
        "🔴 Не передано дизайнеру — в срок не успеем. Нужно решение: показать без дизайна "
        "или договориться о переносе.\n" + _lines(posts) + _tag(ctx),
        _buttons(
            posts, lambda p: stage_button(p.id, Stage.AT_DESIGNER, f"✓ №{p.id} передан дизайнеру")
        ),
        post_ids=[p.id for p in posts],
        kind="remind",
    )


def _show_late_posts(ctx: Ctx) -> list[Post]:
    return ctx.where(
        lambda p: ctx.chain(p).show_day == ctx.today and Stage.TAKEN <= p.stage < Stage.SHOWN_DESIGN
    )


def _show_reminder(ctx: Ctx) -> Reply | None:
    posts = _show_late_posts(ctx)
    if not posts:
        return None
    chain = ctx.chain(posts[0])
    return Reply(
        f"📨 Готовый пост нужно показать клиенту до {chain.show_by:%H:%M}:\n"
        + _lines(posts)
        + _tag(ctx),
        _buttons(
            posts, lambda p: stage_button(p.id, Stage.SHOWN_DESIGN, f"✓ №{p.id} показан клиенту")
        ),
        post_ids=[p.id for p in posts],
        kind="remind",
    )


def _show_overdue(ctx: Ctx) -> Reply | None:
    posts = _show_late_posts(ctx)
    if not posts:
        return None
    return Reply(
        "🔴 Просрочка: готовый пост не показан клиенту.\n" + _lines(posts),
        _buttons(
            posts, lambda p: stage_button(p.id, Stage.SHOWN_DESIGN, f"✓ №{p.id} показан клиенту")
        ),
        post_ids=[p.id for p in posts],
        kind="remind",
    )


def _final(ctx: Ctx) -> Reply | None:
    posts = ctx.where(lambda p: p.publish_date == ctx.today and p.stage < Stage.PUBLISHED)
    if not posts:
        return None
    lines = []
    for post in posts:
        note = (
            "финальный ок есть, а «вышел» не отмечено"
            if post.stage == Stage.FINAL_OK
            else "нет финального ока"
        )
        lines.append(f"• {fmt.line(post)} — {note}")
    buttons: list[list[Button]] = []
    for post in posts[:MAX_BUTTONS]:
        row = []
        if post.stage < Stage.FINAL_OK:
            row.append(stage_button(post.id, Stage.FINAL_OK, f"✅ №{post.id} финальный ок"))
        row.append(stage_button(post.id, Stage.PUBLISHED, f"🚀 №{post.id} вышел"))
        buttons.append(row)
    return Reply(
        "📆 Сегодня выход. Нужен финальный ок клиента, а пост должен быть опубликован или стоять "
        "в отложенных:\n" + "\n".join(lines) + _tag(ctx),
        buttons,
        post_ids=[p.id for p in posts],
        kind="remind",
    )


def _weekly_report(ctx: Ctx) -> Reply | None:
    if not last_workday_of_week(ctx.tracker, ctx.today):
        return None
    return build_weekly_report(ctx.tracker, ctx.now)


# --- правила по КП ---------------------------------------------------------------------------


def _kp(ctx: Ctx):
    return kp_status(ctx.tracker, ctx.today, ctx.parsed_kp)


def _kp_start(ctx: Ctx) -> Reply | None:
    status = _kp(ctx)
    timeline = status.timeline
    if ctx.today != timeline.start_date:
        return None
    return Reply(
        f"📅 Старт КП на {status.month_name}. Ок ответственного — до "
        f"{fmt.wd_date(timeline.ok_deadline)}, показ клиенту — {fmt.wd_date(timeline.show_date)}. "
        "Темы первых дней месяца лучше согласовать первыми: их придётся брать почти сразу "
        "после показа.\nКто собирает?",
        [[Button("Беру сборку", f"kp:own:{status.month_key}")]],
        kind="kp",
    )


def _kp_ok_due(ctx: Ctx) -> Reply | None:
    status = _kp(ctx)
    if ctx.today != status.timeline.ok_deadline or status.ok_at:
        return None
    return Reply(
        f"📅 КП на {status.month_name}: финальный ок нужен до конца дня — завтра показ клиенту."
        f"{_tag(ctx)}",
        [[Button("Ок дан", f"kp:ok:{status.month_key}")]],
        kind="kp",
    )


def _kp_ok_overdue(ctx: Ctx) -> Reply | None:
    status = _kp(ctx)
    if ctx.today != status.timeline.ok_deadline or status.ok_at:
        return None
    return Reply(
        f"🔴 КП на {status.month_name}: финального ока пока нет, а показ клиенту — "
        f"{fmt.wd_date(status.timeline.show_date)}.",
        [[Button("Ок дан", f"kp:ok:{status.month_key}")]],
        kind="kp",
    )


def _kp_show(ctx: Ctx) -> Reply | None:
    status = _kp(ctx)
    if ctx.today != status.timeline.show_date or status.shown_at:
        return None
    return Reply(
        f"📅 Сегодня показываем клиенту КП на {status.month_name}.{_tag(ctx)}",
        [[Button("Показали клиенту", f"kp:shown:{status.month_key}")]],
        kind="kp",
    )


def _kp_show_overdue(ctx: Ctx) -> Reply | None:
    status = _kp(ctx)
    if ctx.today != status.timeline.show_date or status.shown_at:
        return None
    return Reply(
        f"🔴 КП на {status.month_name} сегодня не показано клиенту.",
        [[Button("Показали клиенту", f"kp:shown:{status.month_key}")]],
        kind="kp",
    )


@dataclass(frozen=True)
class Rule:
    id: str
    at: str  # имя настройки времени в config.schedule
    build: Callable[[Ctx], Reply | None]
    escalation: bool = True  # в мягком старте не отправляется


RULES: list[Rule] = [
    Rule("summary", "summary", _summary, escalation=False),
    Rule("kp_start", "summary", _kp_start, escalation=False),
    Rule("take", "take_reminder", _take),
    Rule("text_noon", "text_reminder", _text_noon),
    Rule("text_overdue", "text_overdue", _text_overdue),
    Rule("ok_nudge", "ok_nudge", _ok_nudge),
    Rule("handoff_hard", "handoff_deadline", _handoff_hard),
    Rule("show_reminder", "show_reminder", _show_reminder),
    Rule("show_overdue", "show_overdue", _show_overdue),
    Rule("final", "final_reminder", _final),
    Rule("kp_ok_due", "kp_reminder", _kp_ok_due),
    Rule("kp_ok_overdue", "kp_overdue", _kp_ok_overdue),
    Rule("kp_show", "kp_reminder", _kp_show),
    Rule("kp_show_overdue", "kp_overdue", _kp_show_overdue),
    Rule("weekly_report", "weekly_report", _weekly_report, escalation=False),
]


def soft_mode(tracker: Tracker, now: datetime) -> bool:
    """Первые дни бот только пишет саммари и ведёт доску, без напоминаний и эскалаций."""
    started = tracker.state.get_meta("first_run")
    if not started:
        return tracker.settings.soft_start_days > 0
    start = date.fromisoformat(started)
    return now.date() < start + timedelta(days=tracker.settings.soft_start_days)


def due_rules(
    tracker: Tracker, now: datetime, parsed_kp: KpParseResult | None = None
) -> list[tuple[Rule, Reply | None]]:
    """Правила, время которых пришло и которые сегодня ещё не отрабатывали.

    Второй элемент — сообщение или None, если сказать нечего. После отправки (или
    решения промолчать) правило надо отметить через `mark_done`.
    """
    settings = tracker.settings
    today = now.date()
    if not tracker.calendar.is_workday(today) and not settings.weekend_reminders:
        return []
    ctx = Ctx(
        tracker=tracker,
        now=now,
        parsed_kp=parsed_kp,
        soft=soft_mode(tracker, now),
        posts=tracker.open_posts(today, back_days=3),
    )
    result = []
    for rule in RULES:
        scheduled = datetime.combine(today, getattr(settings.schedule, rule.at), tzinfo=now.tzinfo)
        grace = (
            settings.schedule.summary_catch_up_minutes
            if rule.id == "summary"
            else settings.schedule.catch_up_minutes
        )
        if not scheduled <= now < scheduled + timedelta(minutes=grace):
            continue
        if tracker.state.reminder_sent(rule.id, today):
            continue
        if ctx.soft and rule.escalation:
            result.append((rule, None))
            continue
        try:
            result.append((rule, rule.build(ctx)))
        except Exception:
            # Ошибка в одном правиле не должна лишить команду остальных напоминаний.
            log.exception("Правило %s не сработало", rule.id)
            result.append((rule, None))
    return result


def mark_done(tracker: Tracker, rule: Rule, now: datetime, reply: Reply | None) -> None:
    tracker.state.mark_reminder(rule.id, now.date(), now)
    if reply is not None and not reply.is_empty():
        # Отдельная отметка для отчёта: сколько напоминаний реально ушло.
        count = len(reply.post_ids) or 1
        tracker.state.mark_reminder(rule.id, now.date(), now, key=f"sent:{count}")
