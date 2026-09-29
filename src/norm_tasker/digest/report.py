"""Пятничный отчёт: метрики процесса за неделю. Про этапы, а не про людей."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from norm_tasker import fmt
from norm_tasker.digest.kp import kp_status
from norm_tasker.kp.models import KpParseResult
from norm_tasker.reply import Reply
from norm_tasker.tracker.models import Post
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import Stage

ESCALATIONS = {
    "take": "не взяли пост к сроку",
    "text_overdue": "текст не показан клиенту вовремя",
    "ok_nudge": "утром перед показом не было ока по тексту",
    "handoff_hard": "не передано дизайнеру к крайнему сроку",
    "show_overdue": "готовый пост не показан клиенту вовремя",
}


@dataclass(frozen=True)
class StageTime:
    at: datetime
    from_chat: bool  # время известно точно: об этапе написали в чате, а не узнали из КП


def stage_times(tracker: Tracker, post_id: int) -> dict[Stage, StageTime]:
    """Когда пост дошёл до каждого из нынешних этапов, восстановленное по журналу.

    Прыжок через этапы («отдал дизайнеру» без ока по тексту) считается пройденным сразу;
    откат назад стирает время этапов, с которых пост вернули.
    """
    reached: dict[Stage, StageTime] = {}
    current = Stage.NEW
    for entry in reversed(tracker.history(post_id, limit=500)):
        if "stage" not in entry.after:
            continue
        new = Stage(entry.after["stage"])
        stamp = StageTime(entry.ts, entry.source in ("chat", "button"))
        if new > current:
            for stage in Stage:
                if current < stage <= new:
                    reached[stage] = stamp
        elif new < current:
            for stage in Stage:
                if new < stage <= current:
                    reached.pop(stage, None)
        current = new
    return reached


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600


def human_hours(hours: float) -> str:
    if hours >= 48:
        return f"{hours / 24:.1f} дня".replace(".", ",")
    if hours >= 10:
        return f"{hours:.0f} ч"
    return f"{hours:.1f} ч".replace(".", ",")


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _week_bounds(today: date) -> tuple[date, date]:
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


def _escalation_counts(tracker: Tracker, start: date, end: date) -> dict[str, int]:
    rows = tracker.db.conn.execute(
        "SELECT rule, key FROM sent_reminders WHERE day BETWEEN ? AND ? AND key LIKE 'sent:%'",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        if row["rule"] in ESCALATIONS:
            counts[row["rule"]] = counts.get(row["rule"], 0) + int(row["key"].split(":")[1])
    return counts


def build_weekly_report(
    tracker: Tracker, now: datetime, parsed_kp: KpParseResult | None = None
) -> Reply | None:
    today = now.date()
    start, end = _week_bounds(today)
    posts: list[Post] = tracker.posts_between(start, end)
    if not posts:
        return None

    text_total = text_ok = design_total = design_ok = 0
    text_reply: list[float] = []
    design_reply: list[float] = []
    design_work: list[float] = []
    published = 0
    for post in posts:
        if post.stage >= Stage.PUBLISHED:
            published += 1
        chain = tracker.chain(post)
        times = stage_times(tracker, post.id)
        if now > chain.text_shown_by:
            stamp = times.get(Stage.TEXT_SHOWN)
            if stamp is None or stamp.from_chat:
                text_total += 1
                text_ok += bool(stamp and stamp.at <= chain.text_shown_by)
        if now > chain.show_by:
            stamp = times.get(Stage.SHOWN_DESIGN)
            if stamp is None or stamp.from_chat:
                design_total += 1
                design_ok += bool(stamp and stamp.at <= chain.show_by)
        for first, last, sink in (
            (Stage.TEXT_SHOWN, Stage.TEXT_OK, text_reply),
            (Stage.SHOWN_DESIGN, Stage.FINAL_OK, design_reply),
            (Stage.AT_DESIGNER, Stage.DESIGN_READY, design_work),
        ):
            begin, end_ = times.get(first), times.get(last)
            if begin and end_ and begin.from_chat and end_.from_chat:
                sink.append(_hours(end_.at - begin.at))

    lines = [f"📊 Итоги недели {start:%d.%m}–{end:%d.%m}", ""]
    lines.append(f"Вышло постов: {published} из {len(posts)}")
    if text_total:
        lines.append(f"✍️ Текст показан клиенту вовремя (D−2): {text_ok} из {text_total}")
    if design_total:
        lines.append(
            f"📨 Готовый пост показан клиенту вовремя (D−1): {design_ok} из {design_total}"
        )

    timing = []
    if (avg := _average(text_reply)) is not None:
        timing.append(f"клиент отвечает по тексту в среднем за {human_hours(avg)}")
    if (avg := _average(design_reply)) is not None:
        timing.append(f"по готовому посту — за {human_hours(avg)}")
    if (avg := _average(design_work)) is not None:
        timing.append(f"дизайн занимает около {human_hours(avg)}")
    if timing:
        lines.append("⏱ " + "; ".join(timing).capitalize())

    counts = _escalation_counts(tracker, start, end)
    if counts:
        parts = [f"{ESCALATIONS[rule]} — {n}" for rule, n in counts.items()]
        lines.append("⚠️ Напоминания недели: " + "; ".join(parts))
    else:
        lines.append("⚠️ Эскалаций на этой неделе не было")

    kp = kp_status(tracker, today, parsed_kp)
    if kp.is_relevant(today):
        parts = []
        parts.append(
            "ок дан" if kp.ok_at else f"ок ещё не дан (срок {fmt.wd_date(kp.timeline.ok_deadline)})"
        )
        parts.append(
            "показано клиенту"
            if kp.shown_at
            else f"показ клиенту {fmt.wd_date(kp.timeline.show_date)}"
        )
        lines.append(f"📅 КП на {kp.month_name}: " + ", ".join(parts))

    return Reply("\n".join(lines), kind="report")
