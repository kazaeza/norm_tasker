"""Трекер постов: чтение, действия из чата, журнал и отмена.

Всё, что меняет пост, идёт через `_apply`: он записывает в журнал, что было и что стало.
На журнале работает отмена и метрики. Изменения из КП тоже пишутся в журнал, но отменить их
нельзя: КП — источник дат и тем, откатывать его состояние из чата бессмысленно.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from norm_tasker.calendar_ru import WorkCalendar
from norm_tasker.config import Role, Settings
from norm_tasker.deadlines import PostChain, compute_chain
from norm_tasker.tracker.db import Database
from norm_tasker.tracker.models import Actor, JournalEntry, Post
from norm_tasker.tracker.stages import LABEL, Stage, normalize
from norm_tasker.tracker.state import State
from norm_tasker.tracker.team import Team

EDITABLE = {
    "publish_date", "kp_date", "slot", "rubric", "topic", "doc_url", "kp_status", "kp_stage_seen",
    "source", "in_kp", "assignee_id", "assignee_name", "assignee_username", "stage", "stage_at",
    "cancelled", "note",
}  # fmt: skip
TEXT_LIMIT = 200


def topic_key(text: str | None) -> str:
    """Тема для сравнения: без регистра, знаков препинания и лишних пробелов."""
    return " ".join(re.sub(r"[^\w\s]", " ", normalize(text)).split())


def dismiss_key(day: date, slot: int, topic: str | None) -> str:
    return f"{day.isoformat()}|{slot}|{topic_key(topic)}"


def _encode(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Stage):
        return int(value)
    return value


@dataclass
class ActionResult:
    ok: bool
    code: str  # ok | unchanged | not_found | published | taken_by_other | later_stage | forbidden
    post: Post | None = None
    entry: JournalEntry | None = None
    previous_assignee: str | None = None


class Tracker:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        calendar: WorkCalendar,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.calendar = calendar
        self._clock = clock
        self.state = State(db)
        self.team = Team(settings, self.state)

    def now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(self.settings.tz)

    def today(self) -> date:
        return self.now().date()

    # --- чтение --------------------------------------------------------------------------
    def _select(self, where: str = "1", params: tuple[Any, ...] = ()) -> list[Post]:
        rows = self.db.conn.execute(
            f"SELECT * FROM posts WHERE {where} ORDER BY publish_date, slot, id", params
        ).fetchall()
        return [Post.from_row(row) for row in rows]

    def get(self, post_id: int) -> Post | None:
        found = self._select("id = ?", (post_id,))
        return found[0] if found else None

    def by_ids(self, ids: list[int]) -> list[Post]:
        if not ids:
            return []
        marks = ", ".join("?" for _ in ids)
        return self._select(f"id IN ({marks})", tuple(ids))

    def posts_between(self, start: date, end: date) -> list[Post]:
        """Все посты периода, кроме убранных из трекера."""
        return self._select(
            "cancelled = 0 AND publish_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())
        )

    def posts_on(self, day: date) -> list[Post]:
        return self.posts_between(day, day)

    def open_posts(
        self, today: date, back_days: int = 2, ahead_days: int | None = None
    ) -> list[Post]:
        """Посты, которые ещё не вышли: сегодняшние, будущие и недавно пропущенные."""
        end = today + timedelta(days=ahead_days if ahead_days is not None else 3650)
        return self._select(
            "cancelled = 0 AND stage < ? AND publish_date BETWEEN ? AND ?",
            (
                int(Stage.PUBLISHED),
                (today - timedelta(days=back_days)).isoformat(),
                end.isoformat(),
            ),
        )

    def free_posts(self, start: date, end: date) -> list[Post]:
        return [
            p
            for p in self.posts_between(start, end)
            if not p.assigned and p.stage < Stage.PUBLISHED
        ]

    def count_posts(self, source: str) -> int:
        row = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE source = ?", (source,)
        ).fetchone()
        return int(row["n"])

    def chain(self, post: Post) -> PostChain:
        return compute_chain(
            post.publish_date, self.calendar, self.settings.deadlines, self.settings.tz
        )

    # --- журнал --------------------------------------------------------------------------
    def _journal(
        self,
        *,
        post_id: int | None,
        actor: Actor | None,
        kind: str,
        before: dict[str, Any],
        after: dict[str, Any],
        source: str,
        msg_link: str | None,
        text: str | None,
        undoable: bool,
    ) -> int:
        cursor = self.db.conn.execute(
            "INSERT INTO journal (ts, post_id, actor_id, actor_name, kind, before, after, source, "
            "msg_link, text, undoable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.now().isoformat(),
                post_id,
                actor.id if actor else None,
                actor.name if actor else None,
                kind,
                json.dumps(before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False),
                source,
                msg_link,
                (text or "")[:TEXT_LIMIT] or None,
                int(undoable),
            ),
        )
        return int(cursor.lastrowid or 0)

    def journal_entry(self, entry_id: int) -> JournalEntry | None:
        row = self.db.conn.execute("SELECT * FROM journal WHERE id = ?", (entry_id,)).fetchone()
        return JournalEntry.from_row(row) if row else None

    def history(self, post_id: int, limit: int = 10) -> list[JournalEntry]:
        rows = self.db.conn.execute(
            "SELECT * FROM journal WHERE post_id = ? ORDER BY id DESC LIMIT ?", (post_id, limit)
        ).fetchall()
        return [JournalEntry.from_row(row) for row in rows]

    def last_undoable(
        self, actor: Actor, within: timedelta = timedelta(hours=24)
    ) -> JournalEntry | None:
        if actor.id is None:
            return None
        row = self.db.conn.execute(
            "SELECT * FROM journal WHERE actor_id = ? AND undoable = 1 AND undone = 0 "
            "AND ts >= ? ORDER BY id DESC LIMIT 1",
            (actor.id, (self.now() - within).isoformat()),
        ).fetchone()
        return JournalEntry.from_row(row) if row else None

    def _apply(
        self,
        post_id: int,
        changes: dict[str, Any],
        *,
        kind: str,
        actor: Actor | None,
        source: str,
        msg_link: str | None = None,
        text: str | None = None,
        undoable: bool = True,
        event: str | None = None,
        journal: bool = True,
    ) -> JournalEntry | None:
        """Меняет поля поста и пишет в журнал. None — менять было нечего или журнал отключён."""
        assert set(changes) <= EDITABLE, set(changes) - EDITABLE
        row = self.db.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        encoded = {key: _encode(value) for key, value in changes.items()}
        before = {key: row[key] for key in encoded}
        diff = {key: value for key, value in encoded.items() if before[key] != value}
        if not diff:
            return None
        now = self.now().isoformat()
        assignments = ", ".join(f"{key} = ?" for key in diff)
        params: list[Any] = [*diff.values(), now]
        sql = f"UPDATE posts SET {assignments}, updated_at = ?"
        if event:
            sql += ", last_event = ?"
            params.append(event[:120])
        params.append(post_id)
        with self.db.transaction():
            self.db.conn.execute(sql + " WHERE id = ?", params)
            entry_id = (
                self._journal(
                    post_id=post_id,
                    actor=actor,
                    kind=kind,
                    before={key: before[key] for key in diff},
                    after=diff,
                    source=source,
                    msg_link=msg_link,
                    text=text,
                    undoable=undoable,
                )
                if journal
                else None
            )
        return self.journal_entry(entry_id) if entry_id else None

    def insert_post(
        self,
        *,
        publish_date: date,
        slot: int,
        rubric: str | None,
        topic: str | None,
        doc_url: str | None = None,
        kp_status: str | None = None,
        kp_stage_seen: int | None = None,
        kp_date: date | None = None,
        source: str,
        in_kp: bool,
        stage: Stage = Stage.NEW,
        actor: Actor | None = None,
        kind: str,
        journal_source: str,
        undoable: bool,
        msg_link: str | None = None,
    ) -> Post:
        now = self.now().isoformat()
        with self.db.transaction():
            cursor = self.db.conn.execute(
                "INSERT INTO posts (publish_date, kp_date, slot, rubric, topic, doc_url, "
                "kp_status, "
                "kp_stage_seen, source, in_kp, stage, stage_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    publish_date.isoformat(),
                    kp_date.isoformat() if kp_date else None,
                    slot,
                    rubric,
                    topic,
                    doc_url,
                    kp_status,
                    kp_stage_seen,
                    source,
                    int(in_kp),
                    int(stage),
                    now if stage > Stage.NEW else None,
                    now,
                    now,
                ),
            )
            post_id = int(cursor.lastrowid or 0)
            self._journal(
                post_id=post_id,
                actor=actor,
                kind=kind,
                before={},
                after={
                    "publish_date": publish_date.isoformat(),
                    "topic": topic,
                    **({"stage": int(stage)} if stage > Stage.NEW else {}),
                },
                source=journal_source,
                msg_link=msg_link,
                text=None,
                undoable=undoable,
            )
        created = self.get(post_id)
        assert created is not None
        return created

    # --- действия из чата --------------------------------------------------------------------
    def claim(
        self,
        post_id: int,
        actor: Actor,
        *,
        source: str = "chat",
        msg_link: str | None = None,
        text: str | None = None,
        steal: bool = False,
    ) -> ActionResult:
        post = self.get(post_id)
        if post is None or post.cancelled:
            return ActionResult(False, "not_found")
        if post.stage >= Stage.PUBLISHED:
            return ActionResult(False, "published", post)
        if post.assigned and not post.owned_by(actor) and not steal:
            return ActionResult(False, "taken_by_other", post, previous_assignee=post.assignee_name)
        changes: dict[str, Any] = {
            "assignee_id": actor.id,
            "assignee_name": actor.name,
            "assignee_username": actor.username,
        }
        if post.stage < Stage.TAKEN:
            changes.update(stage=Stage.TAKEN, stage_at=self.now())
        entry = self._apply(
            post_id, changes, kind="claim", actor=actor, source=source, msg_link=msg_link,
            text=text, event=f"{actor.name}: взял пост",
        )  # fmt: skip
        if entry is None:
            return ActionResult(True, "unchanged", post)
        return ActionResult(True, "ok", self.get(post_id), entry, post.assignee_name)

    def release(
        self,
        post_id: int,
        actor: Actor,
        *,
        source: str = "chat",
        msg_link: str | None = None,
        text: str | None = None,
    ) -> ActionResult:
        post = self.get(post_id)
        if post is None or post.cancelled:
            return ActionResult(False, "not_found")
        if not post.assigned:
            return ActionResult(True, "unchanged", post)
        if not post.owned_by(actor) and actor.role != Role.RESPONSIBLE:
            return ActionResult(False, "forbidden", post, previous_assignee=post.assignee_name)
        changes: dict[str, Any] = {
            "assignee_id": None,
            "assignee_name": None,
            "assignee_username": None,
        }
        if post.stage == Stage.TAKEN:
            changes.update(stage=Stage.NEW, stage_at=None)
        entry = self._apply(
            post_id, changes, kind="release", actor=actor, source=source, msg_link=msg_link,
            text=text, event=f"{actor.name}: отдал пост",
        )  # fmt: skip
        return ActionResult(True, "ok", self.get(post_id), entry, post.assignee_name)

    def set_stage(
        self,
        post_id: int,
        stage: Stage,
        actor: Actor,
        *,
        source: str = "chat",
        msg_link: str | None = None,
        text: str | None = None,
        allow_rollback: bool = False,
    ) -> ActionResult:
        post = self.get(post_id)
        if post is None or post.cancelled:
            return ActionResult(False, "not_found")
        if stage == post.stage:
            return ActionResult(True, "unchanged", post)
        if stage < post.stage and not allow_rollback:
            return ActionResult(False, "later_stage", post)
        changes: dict[str, Any] = {"stage": stage, "stage_at": self.now()}
        # Тот, кто сообщил о готовности текста по ничьему посту, и есть его автор.
        if (
            not post.assigned
            and actor.role == Role.COPYWRITER
            and Stage.TAKEN <= stage <= Stage.TEXT_OK
        ):
            changes.update(
                assignee_id=actor.id, assignee_name=actor.name, assignee_username=actor.username
            )
        entry = self._apply(
            post_id, changes, kind="stage", actor=actor, source=source, msg_link=msg_link,
            text=text, event=f"{actor.name}: {LABEL[stage]}",
        )  # fmt: skip
        return ActionResult(True, "ok", self.get(post_id), entry)

    def move(
        self,
        post_id: int,
        new_date: date,
        actor: Actor,
        *,
        source: str = "chat",
        msg_link: str | None = None,
        text: str | None = None,
    ) -> ActionResult:
        post = self.get(post_id)
        if post is None or post.cancelled:
            return ActionResult(False, "not_found")
        entry = self._apply(
            post_id, {"publish_date": new_date}, kind="move", actor=actor, source=source,
            msg_link=msg_link, text=text, event=f"{actor.name}: перенёс на {new_date:%d.%m}",
        )  # fmt: skip
        return ActionResult(
            entry is not None, "ok" if entry else "unchanged", self.get(post_id), entry
        )

    def cancel(
        self,
        post_id: int,
        actor: Actor | None,
        *,
        source: str = "chat",
        remember: bool = True,
        msg_link: str | None = None,
        text: str | None = None,
        kind: str = "cancel",
        undoable: bool = True,
    ) -> ActionResult:
        """Убирает пост из трекера («не пост»). remember — не создавать его заново из КП."""
        post = self.get(post_id)
        if post is None or post.cancelled:
            return ActionResult(False, "not_found")
        if remember:
            self.state.dismiss(
                dismiss_key(post.kp_date or post.publish_date, post.slot, post.topic), self.now()
            )
        entry = self._apply(
            post_id, {"cancelled": True}, kind=kind, actor=actor, source=source,
            msg_link=msg_link, text=text, undoable=undoable,
        )  # fmt: skip
        return ActionResult(True, "ok", self.get(post_id), entry)

    def add_post(
        self,
        publish_date: date,
        topic: str | None,
        actor: Actor,
        *,
        rubric: str | None = None,
        source: str = "chat",
        msg_link: str | None = None,
    ) -> Post:
        """Пост, которого нет в КП (например, срочный)."""
        slot = 1 + max((p.slot for p in self.posts_on(publish_date)), default=0)
        return self.insert_post(
            publish_date=publish_date, slot=slot, rubric=rubric, topic=topic, source="chat",
            in_kp=False, actor=actor, kind="create", journal_source=source, undoable=True,
            msg_link=msg_link,
        )  # fmt: skip

    def undo(self, entry_id: int, actor: Actor) -> ActionResult:
        """Отменяет действие из журнала, если пост с тех пор не менялся."""
        entry = self.journal_entry(entry_id)
        if entry is None or entry.post_id is None:
            return ActionResult(False, "not_found")
        post = self.get(entry.post_id)
        if post is None:
            return ActionResult(False, "not_found")
        if not entry.undoable:
            return ActionResult(False, "not_undoable", post)
        if entry.undone:
            return ActionResult(False, "already_undone", post)
        author = actor.id is not None and entry.actor_id == actor.id
        if not author and actor.role != Role.RESPONSIBLE:
            return ActionResult(False, "forbidden", post)
        if entry.kind == "create":
            if post.cancelled:
                return ActionResult(False, "changed_since", post)
            revert: dict[str, Any] = {"cancelled": True}
        else:
            row = self.db.conn.execute("SELECT * FROM posts WHERE id = ?", (post.id,)).fetchone()
            if {key: row[key] for key in entry.after} != entry.after:
                return ActionResult(False, "changed_since", post)
            revert = dict(entry.before)
        self._apply(
            post.id, revert, kind="undo", actor=actor, source="button", undoable=False,
            event=f"{actor.name}: отмена",
        )  # fmt: skip
        self.db.conn.execute("UPDATE journal SET undone = 1 WHERE id = ?", (entry.id,))
        return ActionResult(True, "ok", self.get(post.id), entry)
