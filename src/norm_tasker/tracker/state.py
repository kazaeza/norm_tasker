"""Небольшие таблицы состояния: метки, сообщения бота, ожидающие кнопки, журнал напоминаний."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from norm_tasker.config import Role
from norm_tasker.tracker.db import Database
from norm_tasker.tracker.models import Actor


class State:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- метки ---------------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self.db.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self.db.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- участники ----------------------------------------------------------------------
    def remember_member(self, actor: Actor, now: datetime) -> None:
        if actor.id is None:
            return
        self.db.conn.execute(
            "INSERT INTO members (user_id, username, name, role, seen_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, "
            "name = excluded.name, role = excluded.role, seen_at = excluded.seen_at",
            (actor.id, actor.username, actor.name, actor.role.value, now.isoformat()),
        )

    def member_id_by_username(self, username: str) -> int | None:
        row = self.db.conn.execute(
            "SELECT user_id FROM members WHERE username = ? ORDER BY seen_at DESC", (username,)
        ).fetchone()
        return row["user_id"] if row else None

    def member_by_id(self, user_id: int) -> Actor | None:
        row = self.db.conn.execute(
            "SELECT user_id, username, name, role FROM members WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return Actor(row["user_id"], row["name"] or "", row["username"], Role(row["role"]))

    # --- сообщения бота -----------------------------------------------------------------
    def remember_message(
        self, chat_id: int, message_id: int, kind: str, post_ids: list[int], now: datetime
    ) -> None:
        self.db.conn.execute(
            "INSERT OR REPLACE INTO bot_messages (chat_id, message_id, kind, post_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, message_id, kind, json.dumps(post_ids), now.isoformat()),
        )

    def message_posts(self, chat_id: int, message_id: int) -> list[int] | None:
        """Посты, о которых написал бот в этом сообщении; None, если сообщение не его."""
        row = self.db.conn.execute(
            "SELECT post_ids FROM bot_messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        return json.loads(row["post_ids"]) if row else None

    def latest_message(
        self, chat_id: int, kinds: tuple[str, ...], since: datetime
    ) -> tuple[int, list[int]] | None:
        marks = ", ".join("?" for _ in kinds)
        row = self.db.conn.execute(
            f"SELECT message_id, post_ids FROM bot_messages "
            f"WHERE chat_id = ? AND kind IN ({marks}) "
            "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (chat_id, *kinds, since.isoformat()),
        ).fetchone()
        return (row["message_id"], json.loads(row["post_ids"])) if row else None

    # --- действия, которые ждут кнопки ---------------------------------------------------
    def create_pending(
        self, kind: str, payload: dict[str, Any], actor_id: int | None, now: datetime
    ) -> int:
        cursor = self.db.conn.execute(
            "INSERT INTO pending (kind, payload, actor_id, created_at) VALUES (?, ?, ?, ?)",
            (kind, json.dumps(payload, ensure_ascii=False), actor_id, now.isoformat()),
        )
        return int(cursor.lastrowid or 0)

    def get_pending(self, pending_id: int) -> tuple[str, dict[str, Any], int | None] | None:
        row = self.db.conn.execute(
            "SELECT kind, payload, actor_id FROM pending WHERE id = ? AND resolved = 0",
            (pending_id,),
        ).fetchone()
        if not row:
            return None
        return row["kind"], json.loads(row["payload"]), row["actor_id"]

    def resolve_pending(self, pending_id: int) -> None:
        self.db.conn.execute("UPDATE pending SET resolved = 1 WHERE id = ?", (pending_id,))

    # --- журнал напоминаний ---------------------------------------------------------------
    def reminder_sent(self, rule: str, day: date, key: str = "") -> bool:
        row = self.db.conn.execute(
            "SELECT 1 FROM sent_reminders WHERE rule = ? AND day = ? AND key = ?",
            (rule, day.isoformat(), key),
        ).fetchone()
        return row is not None

    def mark_reminder(self, rule: str, day: date, now: datetime, key: str = "") -> None:
        self.db.conn.execute(
            "INSERT OR IGNORE INTO sent_reminders (rule, day, key, sent_at) VALUES (?, ?, ?, ?)",
            (rule, day.isoformat(), key, now.isoformat()),
        )

    # --- «не пост» ---------------------------------------------------------------------------
    def dismiss(self, key: str, now: datetime) -> None:
        self.db.conn.execute(
            "INSERT OR IGNORE INTO dismissed (key, created_at) VALUES (?, ?)",
            (key, now.isoformat()),
        )

    def is_dismissed(self, key: str) -> bool:
        row = self.db.conn.execute("SELECT 1 FROM dismissed WHERE key = ?", (key,)).fetchone()
        return row is not None

    # --- КП по месяцам -------------------------------------------------------------------------
    def kp_month(self, month: date) -> dict[str, Any]:
        row = self.db.conn.execute(
            "SELECT owner_id, owner_name, ok_at, shown_at FROM kp_months WHERE month = ?",
            (month.isoformat(),),
        ).fetchone()
        if not row:
            return {"owner_id": None, "owner_name": None, "ok_at": None, "shown_at": None}
        return dict(row)

    def _kp_update(self, month: date, **fields: Any) -> None:
        self.db.conn.execute(
            "INSERT OR IGNORE INTO kp_months (month) VALUES (?)", (month.isoformat(),)
        )
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.db.conn.execute(
            f"UPDATE kp_months SET {assignments} WHERE month = ?",
            (*fields.values(), month.isoformat()),
        )

    def set_kp_owner(self, month: date, actor: Actor) -> None:
        self._kp_update(month, owner_id=actor.id, owner_name=actor.name)

    def mark_kp_ok(self, month: date, now: datetime) -> None:
        self._kp_update(month, ok_at=now.isoformat())

    def mark_kp_shown(self, month: date, now: datetime) -> None:
        self._kp_update(month, shown_at=now.isoformat())
