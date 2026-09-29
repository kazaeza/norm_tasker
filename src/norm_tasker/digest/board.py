"""Закреплённая «Доска»: посты текущей и следующей недели."""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

from norm_tasker import fmt
from norm_tasker.digest.issues import issues_for
from norm_tasker.digest.kp import kp_status, summary_line
from norm_tasker.kp.models import KpParseResult
from norm_tasker.tracker.models import Post
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import EMOJI, Stage

TELEGRAM_LIMIT = 4096
LEGEND = (
    "⚪️ свободен · ✍️ пишется · 📤 текст у клиента · 👌 текст одобрен · 🎨 у дизайнера · "
    "🖼 дизайн готов · 📨 пост у клиента · ✅ финальный ок · 🚀 вышел"
)


def _who(post: Post) -> str:
    if post.assignee_name:
        return f" — {escape(post.assignee_name)}"
    if post.stage >= Stage.PUBLISHED:
        return ""
    return " — автор не отмечен" if post.stage > Stage.NEW else " — свободен"


def _line(post: Post, mark: str) -> str:
    slot = " (2-й)" if post.slot > 1 else ""
    return (
        f"{EMOJI[post.stage]} {fmt.wd_date(post.publish_date)}{slot} №{post.id} "
        f"{fmt.quoted(post, 45)}{_who(post)}{mark}"
    )


def build_board(tracker: Tracker, now: datetime, parsed_kp: KpParseResult | None = None) -> str:
    today = now.date()
    start = today - timedelta(days=today.weekday())
    posts = tracker.posts_between(start, start + timedelta(days=13))
    marks = {
        i.post.id: " 🔴" if i.level == "red" else " 🟡"
        for i in issues_for(tracker, [p for p in posts if p.stage < Stage.PUBLISHED], now)
    }

    def week_block(title: str, first: int) -> list[str]:
        begin = start + timedelta(days=first)
        end = begin + timedelta(days=6)
        rows = [p for p in posts if begin <= p.publish_date <= end]
        lines = [f"{title} ({begin:%d.%m}–{end:%d.%m})"]
        lines += [_line(p, marks.get(p.id, "")) for p in rows] or ["— постов нет"]
        return lines

    parts = [f"📌 Доска · обновлено {now:%d.%m %H:%M}", ""]
    parts += week_block("Эта неделя", 0)
    parts += ["", *week_block("Следующая неделя", 7)]
    kp = summary_line(kp_status(tracker, today, parsed_kp), today)
    if kp:
        parts += ["", kp]
    text = "\n".join(parts)
    if len(text) + len(LEGEND) + 2 <= TELEGRAM_LIMIT:
        text += "\n\n" + LEGEND
    if len(text) > TELEGRAM_LIMIT:
        text = text[: TELEGRAM_LIMIT - 20].rsplit("\n", 1)[0] + "\n…"
    return text
