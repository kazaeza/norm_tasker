"""Цепочка сроков поста и сроки сборки КП на месяц.

Два срока задала команда: D−2 — текст готов и показан клиенту (копирайтеры), D−1 —
готовый пост с дизайном показан клиенту (ответственный за проект). Остальные бот
рассчитывает между ними с учётом графика дизайнера. D — дата выхода поста, она может
быть и выходным; D−1, D−2 — рабочие дни до неё.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from norm_tasker.calendar_ru import WorkCalendar
from norm_tasker.config import DeadlineSettings, KpSettings
from norm_tasker.tracker.stages import Stage


def at(day: date, moment: time, tz: tzinfo) -> datetime:
    return datetime.combine(day, moment, tzinfo=tz)


@dataclass(frozen=True)
class PostChain:
    publish_date: date
    take_day: date  # D−4
    text_day: date  # D−2
    show_day: date  # D−1
    take_by: datetime  # пост взят: конец дня D−4
    text_shown_by: datetime  # текст готов и показан клиенту: конец дня D−2 (правило команды)
    text_ok_soft: datetime  # ок по тексту, если клиент отвечает быстро: конец дня D−2
    text_ok_warn: datetime  # D−1: ока нет, пора напомнить клиенту
    text_ok_hard: datetime  # D−1: позже дизайн не успевает
    handoff_from: datetime  # D−1: начало окна передачи дизайнеру
    handoff_ideal: datetime  # D−1: конец окна, в которое лучше передать
    handoff_hard: datetime  # D−1: крайний срок передачи дизайнеру
    show_by: datetime  # готовый пост показан клиенту: конец дня D−1 (правило команды)
    final_check: datetime  # D: проверка финального ока и публикации
    publish_by: datetime  # конец дня D

    def stage_due(self, stage: Stage) -> datetime | None:
        """К какому моменту пост должен дойти до этапа."""
        return {
            Stage.TAKEN: self.take_by,
            Stage.TEXT_SHOWN: self.text_shown_by,
            Stage.TEXT_OK: self.text_ok_hard,
            Stage.AT_DESIGNER: self.handoff_hard,
            Stage.DESIGN_READY: self.show_by,
            Stage.SHOWN_DESIGN: self.show_by,
            Stage.FINAL_OK: self.final_check,
            Stage.PUBLISHED: self.publish_by,
        }.get(stage)


def compute_chain(
    publish_date: date, calendar: WorkCalendar, rules: DeadlineSettings, tz: tzinfo
) -> PostChain:
    take_day = calendar.prev_workday(publish_date, rules.take_days_before)
    text_day = calendar.prev_workday(publish_date, rules.text_days_before)
    show_day = calendar.prev_workday(publish_date, rules.show_days_before)
    return PostChain(
        publish_date=publish_date,
        take_day=take_day,
        text_day=text_day,
        show_day=show_day,
        take_by=at(take_day, rules.day_end, tz),
        text_shown_by=at(text_day, rules.day_end, tz),
        text_ok_soft=at(text_day, rules.day_end, tz),
        text_ok_warn=at(show_day, rules.text_ok_warn, tz),
        text_ok_hard=at(show_day, rules.text_ok_hard, tz),
        handoff_from=at(show_day, rules.handoff_from, tz),
        handoff_ideal=at(show_day, rules.handoff_to, tz),
        handoff_hard=at(show_day, rules.handoff_hard, tz),
        show_by=at(show_day, rules.day_end, tz),
        final_check=at(publish_date, rules.final_ok_check, tz),
        publish_by=at(publish_date, time(23, 59), tz),
    )


def design_ready_estimate(
    handed_at: datetime, rules: DeadlineSettings
) -> tuple[datetime, datetime]:
    """Когда дизайн ожидается готовым: от 2 до 3 часов после передачи."""
    return (
        handed_at + timedelta(hours=rules.design_hours_min),
        handed_at + timedelta(hours=rules.design_hours_max),
    )


@dataclass(frozen=True)
class KpTimeline:
    """Сроки КП на месяц: старт сборки, финальный ок ответственного, показ клиенту."""

    month: date  # первое число месяца, на который делается КП
    start_date: date
    ok_deadline: date  # финальный ок ответственного — до конца этого дня
    show_date: date  # показ клиенту


def kp_timeline(month: date, calendar: WorkCalendar, rules: KpSettings) -> KpTimeline:
    """КП на `month` показывают клиенту в show_day предыдущего месяца.

    Если это выходной или праздник — в ближайший следующий рабочий день.
    """
    first = month.replace(day=1)
    prev_month_last = first - timedelta(days=1)
    nominal = prev_month_last.replace(day=min(rules.show_day, prev_month_last.day))
    show = calendar.on_or_after(nominal)
    return KpTimeline(
        month=first,
        start_date=calendar.shift_workdays(show, -rules.start_workdays_before_show),
        ok_deadline=calendar.shift_workdays(show, -rules.ok_workdays_before_show),
        show_date=show,
    )


def next_month(day: date) -> date:
    """Первое число следующего месяца."""
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)
