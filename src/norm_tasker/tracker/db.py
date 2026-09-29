"""SQLite: соединение и схема."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE members (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT,
    role TEXT,
    seen_at TEXT
);

CREATE TABLE posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_date TEXT NOT NULL,
    kp_date TEXT,
    slot INTEGER NOT NULL DEFAULT 1,
    rubric TEXT,
    topic TEXT,
    doc_url TEXT,
    kp_status TEXT,
    kp_stage_seen INTEGER,
    source TEXT NOT NULL DEFAULT 'kp',
    in_kp INTEGER NOT NULL DEFAULT 1,
    assignee_id INTEGER,
    assignee_name TEXT,
    assignee_username TEXT,
    stage INTEGER NOT NULL DEFAULT 0,
    stage_at TEXT,
    cancelled INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    last_event TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX posts_date ON posts (publish_date);

CREATE TABLE journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    post_id INTEGER,
    actor_id INTEGER,
    actor_name TEXT,
    kind TEXT NOT NULL,
    before TEXT,
    after TEXT,
    source TEXT NOT NULL DEFAULT 'chat',
    msg_link TEXT,
    text TEXT,
    undoable INTEGER NOT NULL DEFAULT 1,
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX journal_post ON journal (post_id);

-- Какие посты упомянуты в сообщении бота: чтобы понять, о каком посте ответ «беру».
CREATE TABLE bot_messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    post_ids TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

-- Действия, которые ждут нажатия кнопки (например, «какой из постов имеется в виду?»).
CREATE TABLE pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    actor_id INTEGER,
    created_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sent_reminders (
    rule TEXT NOT NULL,
    day TEXT NOT NULL,
    key TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL,
    PRIMARY KEY (rule, day, key)
);

CREATE TABLE comments (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    post_id INTEGER,
    author TEXT,
    is_client INTEGER NOT NULL DEFAULT 1,
    text TEXT,
    created_at TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0,
    seen_at TEXT NOT NULL
);

-- Посты, которые команда объявила «не постом»: чтобы бот не создавал их снова.
CREATE TABLE dismissed (
    key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE kp_months (
    month TEXT PRIMARY KEY,
    owner_id INTEGER,
    owner_name TEXT,
    ok_at TEXT,
    shown_at TEXT
);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._depth = 0
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            try:
                self.conn.executescript(
                    f"BEGIN;\n{SCHEMA}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
                )
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
        elif version > SCHEMA_VERSION:
            raise RuntimeError(
                f"База создана новой версией бота: схема {version}, ожидалась {SCHEMA_VERSION}"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Транзакция; вложенные вызовы входят в уже открытую."""
        if self._depth:
            self._depth += 1
            try:
                yield self.conn
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN")
        self._depth = 1
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._depth = 0

    def close(self) -> None:
        self.conn.close()
