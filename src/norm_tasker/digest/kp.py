"""КП на следующий месяц: сроки, прогресс, что нужно напомнить."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from norm_tasker.deadlines import KpTimeline, kp_timeline, next_month
from norm_tasker.fmt import MONTHS_NOM, wd_date
from norm_tasker.kp.models import KpParseResult
from norm_tasker.tracker.service import Tracker

ANNOUNCE_DAYS_BEFORE_START = 7  # за сколько дней до старта КП появляется в саммари
NAG_DAYS_AFTER_SHOW = 3  # сколько дней после показа бот ещё напоминает о неотмеченных шагах


@dataclass
class KpStatus:
    timeline: KpTimeline
    owner_name: str | None
    ok_at: str | None
    shown_at: str | None
    sheet_exists: bool
    filled: int | None  # рабочих дней месяца с темой; None — КП ещё не читали
    total: int

    @property
    def month_name(self) -> str:
        return MONTHS_NOM[self.timeline.month.month - 1]

    @property
    def month_key(self) -> str:
        return self.timeline.month.strftime("%Y-%m")

    def phase(self, today: date) -> str:
        """far — рано; upcoming — скоро старт; active — сборка; after — время показа прошло."""
        timeline = self.timeline
        if today < timeline.start_date - timedelta(days=ANNOUNCE_DAYS_BEFORE_START):
            return "far"
        if today < timeline.start_date:
            return "upcoming"
        if today <= timeline.show_date:
            return "active"
        return "after"

    def is_relevant(self, today: date) -> bool:
        phase = self.phase(today)
        if phase in ("upcoming", "active"):
            return True
        # После показа напоминаем недолго: без отметок в чате вечно бубнить незачем.
        return phase == "after" and today <= self.timeline.show_date + timedelta(
            days=NAG_DAYS_AFTER_SHOW
        )


def month_from_key(key: str) -> date:
    year, month = key.split("-")
    return date(int(year), int(month), 1)


def kp_status(tracker: Tracker, today: date, parsed: KpParseResult | None) -> KpStatus:
    month = next_month(today)
    timeline = kp_timeline(month, tracker.calendar, tracker.settings.kp)
    saved = tracker.state.kp_month(month)

    weekdays = [
        month + timedelta(days=i)
        for i in range(31)
        if (month + timedelta(days=i)).month == month.month
        and (month + timedelta(days=i)).weekday() < 5
    ]
    sheet_exists = False
    filled: int | None = None
    if parsed is not None:
        sheet_exists = any(info.month == month for info in parsed.sheets)
        with_topic = {s.date for s in parsed.slots if s.slot == 1 and s.topic}
        filled = sum(1 for day in weekdays if day in with_topic)
    return KpStatus(
        timeline=timeline,
        owner_name=saved["owner_name"],
        ok_at=saved["ok_at"],
        shown_at=saved["shown_at"],
        sheet_exists=sheet_exists,
        filled=filled,
        total=len(weekdays),
    )


def summary_line(status: KpStatus, today: date) -> str | None:
    """Строка про КП для утреннего саммари; None, если о нём пока рано говорить."""
    if not status.is_relevant(today):
        return None
    timeline = status.timeline
    phase = status.phase(today)
    head = f"📅 КП на {status.month_name}"
    if phase == "upcoming":
        return (
            f"{head}: старт {wd_date(timeline.start_date)}, "
            f"ок — до {wd_date(timeline.ok_deadline)}, показ клиенту {wd_date(timeline.show_date)}"
        )
    parts = []
    if status.filled is not None:
        progress = f"темы есть у {status.filled} из {status.total} рабочих дней"
        parts.append(progress if status.sheet_exists else "лист месяца ещё не создан")
    if status.owner_name:
        parts.append(f"собирает {status.owner_name}")
    if not status.ok_at:
        parts.append(f"ок ответственного — до {wd_date(timeline.ok_deadline)}")
    if not status.shown_at:
        parts.append(f"показ клиенту {wd_date(timeline.show_date)}")
    else:
        parts.append("показано клиенту")
    return f"{head}: " + ", ".join(parts)
