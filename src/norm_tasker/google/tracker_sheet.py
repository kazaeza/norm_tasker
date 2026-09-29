"""Копия трекера во внутренней Google-таблице. Клиент её не видит; руками её не правим."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from norm_tasker import fmt
from norm_tasker.google.client import GoogleClient
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import LABEL, NEXT_STEP, Stage

log = logging.getLogger(__name__)

TAB = "Трекер"
HEADER = [
    "№", "Дата выхода", "День", "Слот", "Рубрика", "Тема", "Ответственный", "Этап",
    "Следующий шаг", "Срок шага", "Статус в КП", "Док", "Последнее событие", "Обновлено",
]  # fmt: skip
PAST_DAYS = 14
FUTURE_DAYS = 60


def rows_for(tracker: Tracker, now: datetime) -> list[list[str]]:
    today = now.date()
    posts = tracker.posts_between(
        today - timedelta(days=PAST_DAYS), today + timedelta(days=FUTURE_DAYS)
    )
    rows = [HEADER]
    for post in posts:
        chain = tracker.chain(post)
        next_step, due = "", ""
        if post.stage < Stage.PUBLISHED:
            next_step = NEXT_STEP[post.stage]
            deadline = chain.stage_due(Stage(post.stage + 1))
            due = fmt.dt_short(deadline) if deadline else ""
        rows.append(
            [
                str(post.id),
                post.publish_date.isoformat(),
                fmt.WEEKDAYS[post.publish_date.weekday()],
                str(post.slot),
                post.rubric or "",
                post.topic or "",
                post.assignee_name or "",
                LABEL[post.stage],
                next_step,
                due,
                post.kp_status or ("нет в КП" if not post.in_kp else ""),
                post.doc_url or "",
                post.last_event or "",
                f"{post.updated_at:%d.%m.%Y %H:%M}",
            ]
        )
    return rows


def push_rows(client: GoogleClient, spreadsheet_id: str, rows: list[list[str]]) -> int:
    """Записывает готовые строки на вкладку «Трекер» (только сеть — можно в потоке)."""
    if TAB not in client.sheet_tabs(spreadsheet_id):
        client.add_tab(spreadsheet_id, TAB)
    client.replace_values(spreadsheet_id, TAB, rows)
    return len(rows) - 1


def write_tracker_copy(
    client: GoogleClient, spreadsheet_id: str, tracker: Tracker, now: datetime
) -> int:
    """Обновляет вкладку «Трекер». Возвращает число записанных постов."""
    return push_rows(client, spreadsheet_id, rows_for(tracker, now))
