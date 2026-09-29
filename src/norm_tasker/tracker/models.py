"""Модели трекера."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from norm_tasker.config import Role
from norm_tasker.tracker.stages import Stage

NO_TOPIC = "тема не выбрана"


@dataclass(frozen=True)
class Actor:
    """Участник команды, от чьего имени что-то происходит."""

    id: int | None
    name: str
    username: str | None
    role: Role


@dataclass
class Post:
    id: int
    publish_date: date  # фактическая дата выхода (после переноса в чате может отличаться от КП)
    kp_date: date | None  # дата, которую последний раз видели в КП
    slot: int
    rubric: str | None
    topic: str | None
    doc_url: str | None
    kp_status: str | None
    kp_stage_seen: int | None  # этап по последнему виденному статусу КП
    source: str  # kp | chat
    in_kp: bool
    assignee_id: int | None
    assignee_name: str | None
    assignee_username: str | None
    stage: Stage
    stage_at: datetime | None
    cancelled: bool
    note: str | None
    last_event: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Post:
        return cls(
            id=row["id"],
            publish_date=date.fromisoformat(row["publish_date"]),
            kp_date=date.fromisoformat(row["kp_date"]) if row["kp_date"] else None,
            slot=row["slot"],
            rubric=row["rubric"],
            topic=row["topic"],
            doc_url=row["doc_url"],
            kp_status=row["kp_status"],
            kp_stage_seen=row["kp_stage_seen"],
            source=row["source"],
            in_kp=bool(row["in_kp"]),
            assignee_id=row["assignee_id"],
            assignee_name=row["assignee_name"],
            assignee_username=row["assignee_username"],
            stage=Stage(row["stage"]),
            stage_at=datetime.fromisoformat(row["stage_at"]) if row["stage_at"] else None,
            cancelled=bool(row["cancelled"]),
            note=row["note"],
            last_event=row["last_event"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @property
    def title(self) -> str:
        return self.topic or NO_TOPIC

    @property
    def assigned(self) -> bool:
        return self.assignee_id is not None or self.assignee_name is not None

    @property
    def doc_id(self) -> str | None:
        from norm_tasker.kp.models import doc_id_from_url

        return doc_id_from_url(self.doc_url)

    def owned_by(self, actor: Actor) -> bool:
        if actor.id is not None and self.assignee_id == actor.id:
            return True
        return bool(actor.username) and self.assignee_username == actor.username


@dataclass(frozen=True)
class JournalEntry:
    id: int
    ts: datetime
    post_id: int | None
    actor_id: int | None
    actor_name: str | None
    kind: str
    before: dict[str, Any]
    after: dict[str, Any]
    source: str
    msg_link: str | None
    text: str | None
    undoable: bool
    undone: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> JournalEntry:
        return cls(
            id=row["id"],
            ts=datetime.fromisoformat(row["ts"]),
            post_id=row["post_id"],
            actor_id=row["actor_id"],
            actor_name=row["actor_name"],
            kind=row["kind"],
            before=json.loads(row["before"] or "{}"),
            after=json.loads(row["after"] or "{}"),
            source=row["source"],
            msg_link=row["msg_link"],
            text=row["text"],
            undoable=bool(row["undoable"]),
            undone=bool(row["undone"]),
        )


@dataclass(frozen=True)
class ExternalComment:
    """Комментарий клиента или команды из КП или дока."""

    id: str
    source: str  # kp | doc
    author: str
    text: str
    created: datetime | None
    resolved: bool
    day: date | None = None  # КП: день и слот ячейки
    slot: int | None = None
    doc_id: str | None = None  # док: к какому доку относится


@dataclass(frozen=True)
class NewComment:
    post: Post
    comment: ExternalComment
    is_client: bool


@dataclass
class SyncReport:
    """Что изменилось в трекере после сверки с КП."""

    initial: bool = False
    created: list[Post] = field(default_factory=list)
    moved: list[tuple[Post, date, date]] = field(default_factory=list)
    topic_changed: list[tuple[Post, str | None, str | None]] = field(default_factory=list)
    stage_from_kp: list[tuple[Post, Stage, Stage]] = field(default_factory=list)
    vanished: list[Post] = field(default_factory=list)  # пропали из КП, но по ним есть работа
    removed: list[Post] = field(default_factory=list)  # пропали из КП, работы не было
    slot_to_post: dict[tuple[date, int], int] = field(default_factory=dict)
    # Сколько постов «пропало» из КП сразу: похоже на сломанный шаблон, трекер не трогаем.
    suspicious: int = 0

    @property
    def has_changes(self) -> bool:
        return any(
            (
                self.created,
                self.moved,
                self.topic_changed,
                self.stage_from_kp,
                self.vanished,
                self.removed,
                self.suspicious,
            )
        )
