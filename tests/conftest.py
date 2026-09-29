from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from norm_tasker.calendar_ru import WorkCalendar, build_calendar
from norm_tasker.config import Member, Role, Settings
from norm_tasker.kp.models import KpSlot
from norm_tasker.tracker.db import Database
from norm_tasker.tracker.models import Actor
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.sync import sync_kp

MSK = ZoneInfo("Europe/Moscow")


class Clock:
    """Управляемые часы: тест сам решает, который час."""

    def __init__(self, now: datetime) -> None:
        self.current = now

    def __call__(self) -> datetime:
        return self.current

    def set(self, year: int, month: int, day: int, hour: int = 10, minute: int = 0) -> None:
        self.current = datetime(year, month, day, hour, minute, tzinfo=MSK)


@pytest.fixture(scope="session")
def calendar() -> WorkCalendar:
    return build_calendar()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        chat_id=-1001234567890,
        team=[
            Member(name="Копирайтер Альфа", username="cw_alpha", role=Role.COPYWRITER),
            Member(name="Копирайтер Бета", username="cw_beta", role=Role.COPYWRITER),
            Member(name="Ответственный", username="lead_user", role=Role.RESPONSIBLE),
            Member(name="Руководитель", username="boss_user", role=Role.BOSS),
        ],
        client_authors=["Клиент Один", "Клиент Два"],
        team_authors=["Копирайтер Альфа", "Ответственный"],
    )


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 9, 29, 10, 0, tzinfo=MSK))  # вторник


@pytest.fixture
def tracker(settings, calendar, clock) -> Tracker:
    return Tracker(Database(":memory:"), settings, calendar, clock)


@pytest.fixture
def alpha(tracker) -> Actor:
    actor = tracker.team.identify(101, "Cw_Alpha", "Alpha")
    assert actor is not None
    return actor


@pytest.fixture
def beta(tracker) -> Actor:
    actor = tracker.team.identify(102, "cw_beta", "Beta")
    assert actor is not None
    return actor


@pytest.fixture
def lead(tracker) -> Actor:
    actor = tracker.team.identify(103, "lead_user", "Lead")
    assert actor is not None
    return actor


def make_slot(
    day: date,
    *,
    slot: int = 1,
    topic: str | None = "Тема поста для проверки",
    status: str | None = None,
    rubric: str | None = "Продукт",
    doc: str | None = None,
) -> KpSlot:
    return KpSlot(
        date=day,
        slot=slot,
        rubric=rubric,
        status=status,
        topic=topic,
        doc_url=f"https://docs.google.com/document/d/{doc}/edit" if doc else None,
        sheet="Октябрь 2026",
        cell="A5",
    )


MON_PAST = date(2026, 9, 28)
WED = date(2026, 9, 30)
THU = date(2026, 10, 1)
FRI = date(2026, 10, 2)
MON = date(2026, 10, 5)
FRI2 = date(2026, 10, 9)


@pytest.fixture
def board(tracker):
    """Сегодня вторник 29.09; пост за понедельник уже вышел."""
    sync_kp(
        tracker,
        [
            make_slot(MON_PAST, topic="Продуктовый пост про суперлайк", status="Выпущено"),
            make_slot(WED, topic="Карточки про фразу, которая сближает", rubric="Информационный"),
            make_slot(THU, topic=None, rubric="Развлекательный"),
            make_slot(FRI, topic="11 примеров флирта на свидании", rubric="Информационный"),
            make_slot(MON, topic=None, rubric="Развлекательный"),
            make_slot(FRI2, topic="Мем про выбор фильма и профессии", rubric="Развлекательный"),
        ],
    )
    return {p.publish_date: p for p in tracker._select()}


@pytest.fixture
def telegram():
    """Подставной сервер Telegram на localhost (см. tests/tg_fixture.py)."""
    from tg_fixture import FakeTelegram

    server = FakeTelegram()
    yield server
    server.close()
