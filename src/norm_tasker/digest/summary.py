"""Утреннее саммари: одно сообщение вместо потока напоминаний."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape

from norm_tasker import fmt
from norm_tasker.config import Role
from norm_tasker.digest.issues import Issue, issues_for
from norm_tasker.digest.kp import kp_status, summary_line
from norm_tasker.kp.models import KpParseResult
from norm_tasker.reply import Button, Reply, claim_button
from norm_tasker.tracker.models import Post
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import NEXT_STEP, Stage
from norm_tasker.tracker.sync import pending_comment_notifications

LIMIT = 8  # строк в разделе; остальное сворачиваем в «…и ещё N»
FREE_WINDOW_DAYS = 14
FREE_BUTTONS = 8
COMMENT_LIMIT = 5


def first_workday_of_week(tracker: Tracker, day: date) -> bool:
    calendar = tracker.calendar
    if not calendar.is_workday(day):
        return False
    return calendar.prev_workday(day).isocalendar()[:2] != day.isocalendar()[:2]


def last_workday_of_week(tracker: Tracker, day: date) -> bool:
    calendar = tracker.calendar
    if not calendar.is_workday(day):
        return False
    return calendar.next_workday(day).isocalendar()[:2] != day.isocalendar()[:2]


def issue_line(issue: Issue) -> str:
    post = issue.post
    who = f" ({escape(post.assignee_name)})" if post.assignee_name else ""
    return f"• {fmt.wd_date(post.publish_date)} {fmt.label(post)}{who} — {issue.text}"


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= LIMIT:
        return lines
    return [*lines[:LIMIT], f"…и ещё {len(lines) - LIMIT}"]


def _design_today(tracker: Tracker, posts: list[Post], today: date) -> str | None:
    items = []
    for post in posts:
        chain = tracker.chain(post)
        if chain.show_day != today:
            continue
        if post.stage == Stage.TEXT_OK:
            items.append(f"{fmt.label(post, 40)} — передать до {chain.handoff_ideal:%H:%M}")
        elif post.stage == Stage.TEXT_SHOWN:
            items.append(f"{fmt.label(post, 40)} — как только придёт ок по тексту")
        elif post.stage in (Stage.AT_DESIGNER, Stage.DESIGN_READY):
            items.append(f"{fmt.label(post, 40)} — уже у дизайнера")
    return "🎨 Дизайнеру сегодня: " + "; ".join(items) if items else None


def design_load_warning(tracker: Tracker, posts: list[Post], today: date) -> str | None:
    """Если на следующий рабочий день приходится несколько постов на дизайн."""
    tomorrow = tracker.calendar.next_workday(today)
    queue = [
        p for p in posts if tracker.chain(p).show_day == tomorrow and p.stage < Stage.DESIGN_READY
    ]
    if len(queue) < 2:
        return None
    chain = tracker.chain(queue[0])
    names = ", ".join(fmt.label(p, 30) for p in queue)
    count = len(queue)
    return (
        f"🎨 {fmt.wd_date(tomorrow)} дизайнеру {count} "
        f"{fmt.plural(count, 'пост', 'поста', 'постов')}: {names}. "
        f"Уже одобренные тексты лучше передать сегодня до {chain.handoff_ideal:%H:%M}."
    )


def free_posts_block(
    tracker: Tracker, today: date
) -> tuple[str, list[list[Button]], list[int]] | None:
    free = [
        p
        for p in tracker.free_posts(today, today + timedelta(days=FREE_WINDOW_DAYS))
        if p.stage < Stage.PUBLISHED
    ]
    if not free:
        return None
    lines = [f"• {fmt.line(p)}" + (f" — {escape(p.rubric)}" if p.rubric else "") for p in free]
    count = len(free)
    text = f"🆓 Без ответственного на 2 недели ({count}):\n" + "\n".join(_capped(lines))
    picked = free[:FREE_BUTTONS]
    buttons = [
        [
            claim_button(p.id, f"Беру №{p.id} · {fmt.wd_date(p.publish_date)}")
            for p in picked[i : i + 2]
        ]
        for i in range(0, len(picked), 2)
    ]
    return text, buttons, [p.id for p in picked]


def in_progress_block(
    tracker: Tracker, posts: list[Post], flagged: set[int], today: date, *, tag: bool
) -> str | None:
    """Первый рабочий день недели: что уже в работе — кто что делает и к какому сроку.

    Посты, о которых в саммари уже сказано в «Горит» и «Сегодня и скоро» (`flagged`), не повторяем.
    """
    horizon = today + timedelta(days=FREE_WINDOW_DAYS)
    working = [
        p
        for p in posts
        if p.assigned and p.id not in flagged and today <= p.publish_date <= horizon
    ]
    if not working:
        return None
    lines = []
    for post in working:
        who = tracker.team.mention_assignee(post) if tag else escape(post.assignee_name or "")
        due = tracker.chain(post).stage_due(Stage(post.stage + 1))
        step = f"; {NEXT_STEP[post.stage]} — до {fmt.dt_short(due)}" if due else ""
        lines.append(f"• {fmt.line(post)} — {who}, {fmt.stage_text(post.stage)}{step}")
    return f"📋 В работе на 2 недели ({len(working)}):\n" + "\n".join(_capped(lines))


def comments_block(tracker: Tracker) -> tuple[str | None, list[str]]:
    notes = pending_comment_notifications(tracker)
    if not notes:
        return None, []
    lines = []
    for note in notes[:COMMENT_LIMIT]:
        where = "док" if note.comment.source == "doc" else "КП"
        lines.append(
            f"💬 {escape(note.comment.author)} ({where}): {fmt.line(note.post, 40)} — "
            f"«{escape(fmt.clip(note.comment.text, 120))}»"
        )
    if len(notes) > COMMENT_LIMIT:
        lines.append(f"…и ещё {len(notes) - COMMENT_LIMIT}")
    return "\n".join(lines), [n.comment.id for n in notes]


def build_summary(
    tracker: Tracker,
    now: datetime,
    parsed_kp: KpParseResult | None = None,
    *,
    soft: bool = False,
) -> tuple[Reply, list[str]]:
    """Текст саммари и id комментариев, о которых в нём сказано (их нужно отметить прочитанными)."""
    today = now.date()
    posts = tracker.open_posts(today, back_days=3)
    issues = issues_for(tracker, posts, now)
    tag = not soft

    sections: list[str] = []
    red_issues = [i for i in issues if i.level == "red"]
    red = [issue_line(i) for i in red_issues]
    yellow = [issue_line(i) for i in issues if i.level == "yellow"]
    if red:
        block = "🔴 Горит\n" + "\n".join(_capped(red))
        needs_lead = any(i.role == Role.RESPONSIBLE for i in red_issues)
        if tag and needs_lead and tracker.team.mention_responsible():
            block += f"\n{tracker.team.mention_responsible()}"
        sections.append(block)
    if yellow:
        sections.append("🟡 Сегодня и скоро\n" + "\n".join(_capped(yellow)))

    design = _design_today(tracker, posts, today)
    if design:
        sections.append(design)
    load = design_load_warning(tracker, posts, today)
    if load:
        sections.append(load)

    publishing = [
        f"{fmt.label(p, 40)}"
        for p in posts
        if p.publish_date == today and p.stage == Stage.FINAL_OK
    ]
    if publishing:
        sections.append("✅ Сегодня выходит: " + "; ".join(publishing) + " — финальный ок есть")

    buttons: list[list[Button]] = []
    post_ids: list[int] = []
    if first_workday_of_week(tracker, today):
        working = in_progress_block(tracker, posts, {i.post.id for i in issues}, today, tag=tag)
        if working:
            sections.append(working)
        block = free_posts_block(tracker, today)
        if block:
            text, buttons, post_ids = block
            sections.append(text)

    kp = summary_line(kp_status(tracker, today, parsed_kp), today)
    if kp:
        sections.append(kp)

    comments, comment_ids = comments_block(tracker)
    if comments:
        sections.append(comments)

    body = "\n\n".join(sections) if sections else "🟢 Срочного нет."
    text = f"☀️ {fmt.long_date(today)}\n\n{body}"
    return Reply(text, buttons, post_ids=post_ids, kind="summary"), comment_ids
