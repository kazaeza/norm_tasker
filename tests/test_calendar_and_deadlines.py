from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from norm_tasker.calendar_ru import WorkCalendar, build_calendar, is_valid_year_string
from norm_tasker.config import DeadlineSettings, KpSettings
from norm_tasker.deadlines import compute_chain, kp_timeline, next_month
from norm_tasker.tracker.stages import Stage

MSK = ZoneInfo("Europe/Moscow")
RULES = DeadlineSettings()


@pytest.fixture(scope="module")
def cal() -> WorkCalendar:
    return build_calendar()


def test_holidays_of_2026(cal):
    days_off = [
        date(2026, 1, 1), date(2026, 1, 9), date(2026, 2, 23), date(2026, 3, 9),
        date(2026, 5, 1), date(2026, 5, 11), date(2026, 6, 12), date(2026, 11, 4),
        date(2026, 12, 31),
    ]  # fmt: skip
    for day in days_off:
        assert not cal.is_workday(day), day
    assert cal.is_workday(date(2026, 10, 26))
    assert not cal.is_workday(date(2026, 10, 25))  # воскресенье
    assert cal.is_short_day(date(2026, 11, 3))


def test_year_without_data_falls_back_to_weekdays(cal):
    assert not cal.has_year(2031)
    assert cal.is_workday(date(2031, 1, 1))  # чт, данных нет — считаем рабочим
    assert not cal.is_workday(date(2031, 1, 4))  # сб


def test_invalid_year_strings_are_rejected():
    assert not is_valid_year_string(2027, "0" * 365)  # источник отдаёт нули, когда данных нет
    assert not is_valid_year_string(2026, "1" * 100)
    calendar = WorkCalendar()
    assert not calendar.set_year(2027, "0" * 365)
    assert not calendar.has_year(2027)


def test_manual_overrides_win():
    calendar = WorkCalendar(
        extra_days_off=[date(2026, 10, 14)], extra_workdays=[date(2026, 10, 17)]
    )
    assert not calendar.is_workday(date(2026, 10, 14))  # среда объявлена выходным
    assert calendar.is_workday(date(2026, 10, 17))  # суббота объявлена рабочей


def test_shift_workdays_skips_holidays(cal):
    # Пост на 12 мая (вт): D−1 — пятница 8 мая (11 мая — выходной).
    assert cal.prev_workday(date(2026, 5, 12)) == date(2026, 5, 8)
    # Новогодние каникулы: перед 12 января последний рабочий день — 30 декабря 2025.
    assert cal.prev_workday(date(2026, 1, 12)) == date(2025, 12, 30)
    assert cal.shift_workdays(date(2026, 10, 23), 1) == date(2026, 10, 26)
    assert cal.shift_workdays(date(2026, 10, 26), 0) == date(2026, 10, 26)


# Таблица из PLAN.md: день выхода → когда взять / текст / дизайн и показ.
WEEKDAY_TABLE = [
    (date(2026, 10, 12), date(2026, 10, 6), date(2026, 10, 8), date(2026, 10, 9)),  # пн
    (date(2026, 10, 13), date(2026, 10, 7), date(2026, 10, 9), date(2026, 10, 12)),  # вт
    (date(2026, 10, 14), date(2026, 10, 8), date(2026, 10, 12), date(2026, 10, 13)),  # ср
    (date(2026, 10, 15), date(2026, 10, 9), date(2026, 10, 13), date(2026, 10, 14)),  # чт
    (date(2026, 10, 16), date(2026, 10, 12), date(2026, 10, 14), date(2026, 10, 15)),  # пт
    (date(2026, 10, 17), date(2026, 10, 13), date(2026, 10, 15), date(2026, 10, 16)),  # сб
    (date(2026, 10, 18), date(2026, 10, 13), date(2026, 10, 15), date(2026, 10, 16)),  # вс
]


@pytest.mark.parametrize(("publish", "take", "text", "show"), WEEKDAY_TABLE)
def test_chain_matches_plan_table(cal, publish, take, text, show):
    chain = compute_chain(publish, cal, RULES, MSK)
    assert (chain.take_day, chain.text_day, chain.show_day) == (take, text, show)


def test_chain_times(cal):
    chain = compute_chain(date(2026, 10, 16), cal, RULES, MSK)  # пятница
    assert chain.take_by == datetime(2026, 10, 12, 18, 0, tzinfo=MSK)
    assert chain.text_shown_by == datetime(2026, 10, 14, 18, 0, tzinfo=MSK)
    assert chain.text_ok_warn == datetime(2026, 10, 15, 11, 0, tzinfo=MSK)
    assert chain.text_ok_hard == datetime(2026, 10, 15, 13, 0, tzinfo=MSK)
    assert chain.handoff_from == datetime(2026, 10, 15, 9, 0, tzinfo=MSK)
    assert chain.handoff_ideal == datetime(2026, 10, 15, 10, 0, tzinfo=MSK)
    assert chain.show_by == datetime(2026, 10, 15, 18, 0, tzinfo=MSK)
    assert chain.final_check == datetime(2026, 10, 16, 12, 0, tzinfo=MSK)
    assert chain.stage_due(Stage.TEXT_SHOWN) == chain.text_shown_by
    assert chain.stage_due(Stage.NEW) is None


def test_chain_honours_custom_settings(cal):
    rules = DeadlineSettings(day_end=time(17, 0), take_days_before=5)
    chain = compute_chain(date(2026, 10, 16), cal, rules, MSK)
    assert chain.take_day == date(2026, 10, 9)
    assert chain.show_by.hour == 17


def test_kp_timeline_for_november_2026(cal):
    # 25.10.2026 — воскресенье: показ переносится на пн 26.10, ок — пт 23.10, старт — вт 20.10.
    timeline = kp_timeline(date(2026, 11, 1), cal, KpSettings())
    assert timeline.show_date == date(2026, 10, 26)
    assert timeline.ok_deadline == date(2026, 10, 23)
    assert timeline.start_date == date(2026, 10, 20)


def test_kp_timeline_when_25th_is_a_workday(cal):
    # 25.09.2026 — пятница: показ в этот же день.
    timeline = kp_timeline(date(2026, 10, 1), cal, KpSettings())
    assert timeline.show_date == date(2026, 9, 25)
    assert timeline.ok_deadline == date(2026, 9, 24)
    assert timeline.start_date == date(2026, 9, 21)


def test_kp_timeline_january_crosses_year(cal):
    timeline = kp_timeline(date(2027, 1, 1), cal, KpSettings())
    assert timeline.show_date.year == 2026
    assert timeline.show_date >= date(2026, 12, 25)


def test_next_month():
    assert next_month(date(2026, 9, 29)) == date(2026, 10, 1)
    assert next_month(date(2026, 12, 31)) == date(2027, 1, 1)
