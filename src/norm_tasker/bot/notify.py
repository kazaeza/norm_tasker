"""Сообщения по итогам сверки с КП и по комментариям клиента."""

from __future__ import annotations

from datetime import datetime
from difflib import SequenceMatcher
from html import escape

from norm_tasker import fmt
from norm_tasker.reply import Button, Reply
from norm_tasker.tracker.models import Post, SyncReport
from norm_tasker.tracker.service import Tracker, topic_key
from norm_tasker.tracker.stages import Stage
from norm_tasker.tracker.sync import pending_comment_notifications

DAY_START_HOUR = 9
DAY_END_HOUR = 20
REPLACED_BELOW = 0.6  # похожесть тем, ниже которой считаем, что тему заменили
COMMENT_LIMIT = 5


def _worked_on(post: Post) -> bool:
    return post.assigned or post.stage > Stage.NEW


def sync_messages(tracker: Tracker, report: SyncReport) -> list[Reply]:
    """Что стоит сказать команде о том, что изменилось в КП."""
    replies: list[Reply] = []
    if report.initial:
        return replies

    moved = [(p, old, new) for p, old, new in report.moved if _worked_on(p)]
    if moved:
        lines = []
        people: list[str] = []
        for before, old, new in moved:
            post = tracker.get(before.id) or before
            chain = tracker.chain(post)
            lines.append(
                f"• {fmt.label(post, 45)}: {fmt.wd_date(old)} → {fmt.wd_date(new)}; "
                f"текст клиенту — до {fmt.dt_short(chain.text_shown_by)}, "
                f"показ с дизайном — до {fmt.dt_short(chain.show_by)}"
            )
            mention = tracker.team.mention_assignee(post)
            if mention:
                people.append(mention)
        tail = " ".join(dict.fromkeys(people))
        replies.append(
            Reply(
                "📆 В КП перенесли посты, сроки пересчитаны:\n"
                + "\n".join(lines)
                + (f"\n{tail}" if tail else ""),
                post_ids=[p.id for p, _, _ in moved],
                kind="notice",
            )
        )

    for before, old, new in report.topic_changed:
        if not _worked_on(before) or not old or not new:
            continue
        ratio = SequenceMatcher(None, topic_key(old), topic_key(new)).ratio()
        if ratio >= REPLACED_BELOW:
            continue
        post = tracker.get(before.id) or before
        mention = tracker.team.mention_assignee(post)
        replies.append(
            Reply(
                f"✏️ В КП изменилась тема поста №{post.id} ({fmt.wd_date(post.publish_date)}):\n"
                f"было: «{escape(fmt.clip(old, 80))}»\n"
                f"стало: «{escape(fmt.clip(new, 80))}»\n"
                f"Этап сейчас — {fmt.stage_text(post.stage)}. {mention}".strip(),
                post_ids=[post.id],
                kind="notice",
            )
        )

    for before in report.vanished:
        post = tracker.get(before.id) or before
        who = f", автор {escape(post.assignee_name)}" if post.assignee_name else ""
        replies.append(
            Reply(
                f"❓ Пост {fmt.line(post)} пропал из КП, а по нему есть работа{who} "
                f"(этап — {fmt.stage_text(post.stage)}). Убрать из трекера или оставить?\n"
                f"{tracker.team.mention_responsible()}".strip(),
                [
                    [
                        Button("Убрать из трекера", f"vanish:remove:{post.id}"),
                        Button("Оставить", f"vanish:keep:{post.id}"),
                    ]
                ],
                post_ids=[post.id],
                kind="ask",
            )
        )

    if report.suspicious:
        replies.append(
            Reply(
                f"⚠️ В КП разом пропало {report.suspicious} "
                f"{fmt.plural(report.suspicious, 'пост', 'поста', 'постов')} — похоже, изменился "
                "шаблон листа или файл. Трекер не трогаю; проверьте КП.",
                kind="notice",
            )
        )
    return replies


def comment_reply(tracker: Tracker, now: datetime) -> Reply | None:
    """Новые комментарии клиента — днём и в рабочий день; ночью их заберёт утреннее саммари."""
    if not tracker.calendar.is_workday(now.date()):
        return None
    if not DAY_START_HOUR <= now.hour < DAY_END_HOUR:
        return None
    notes = pending_comment_notifications(tracker)
    if not notes:
        return None
    lines = []
    for note in notes[:COMMENT_LIMIT]:
        where = "док" if note.comment.source == "doc" else "КП"
        lines.append(
            f"• {escape(note.comment.author)} ({where}) — {fmt.line(note.post, 40)}: "
            f"«{escape(fmt.clip(note.comment.text, 140))}»"
        )
    if len(notes) > COMMENT_LIMIT:
        lines.append(f"…и ещё {len(notes) - COMMENT_LIMIT}")
    ids = [n.comment.id for n in notes]
    return Reply(
        "💬 Новые комментарии клиента:\n" + "\n".join(lines),
        post_ids=[n.post.id for n in notes],
        kind="notice",
        meta={"comment_ids": ids},
    )
