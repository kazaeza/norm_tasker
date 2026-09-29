"""Сборка и запуск бота: Telegram, чтение КП, напоминания, доска, копия трекера."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from datetime import datetime, timedelta
from html import escape
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner

from norm_tasker.bot.handlers import Handlers
from norm_tasker.bot.notify import comment_reply, sync_messages
from norm_tasker.bot.telegram import Sender
from norm_tasker.bot.views import RuntimeInfo
from norm_tasker.calendar_ru import build_calendar, refresh_calendar
from norm_tasker.config import Env, Settings
from norm_tasker.digest.board import build_board
from norm_tasker.digest.reminders import due_rules, mark_done
from norm_tasker.google.client import GoogleClient, GoogleError
from norm_tasker.google.tracker_sheet import push_rows, rows_for
from norm_tasker.kp.parser import parse_kp
from norm_tasker.reply import Reply
from norm_tasker.tracker.db import Database
from norm_tasker.tracker.models import ExternalComment
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.sync import mark_comments_notified, record_comments, sync_kp

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]
TICK_SECONDS = 30
BOARD_REFRESH_SECONDS = 1800
SHEET_MIN_INTERVAL = 60
SHEET_REFRESH_SECONDS = 600
CALENDAR_REFRESH_SECONDS = 24 * 3600
DOWNTIME_HOURS = 20  # Telegram хранит апдейты для бота 24 часа
KP_FAILURES_BEFORE_ALERT = 6
FIRST_SYNC_WAIT_MINUTES = 5


class App:
    def __init__(
        self,
        env: Env,
        settings: Settings,
        bot: Bot,
        tracker: Tracker,
        google: GoogleClient | None = None,
    ) -> None:
        self.env = env
        self.settings = settings
        self.bot = bot
        self.tracker = tracker
        self.google = google
        self.sender = Sender(bot, tracker, settings)
        self.info = RuntimeInfo(
            google_configured=google is not None and bool(env.kp_spreadsheet_id),
            tracker_copy_configured=google is not None and bool(env.tracker_spreadsheet_id),
        )
        self.parsed_kp = None
        self.bot_id: int | None = None
        self.dp = Dispatcher()
        self.handlers = Handlers(self)
        self.handlers.register(self.dp)
        self._kp_hash: str | None = None
        self._kp_failures = 0
        self._board_failures = 0
        self._board_dirty = asyncio.Event()
        self._sheet_dirty = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []

    # --- служебное ----------------------------------------------------------------------
    def mark_dirty(self) -> None:
        """Состояние трекера изменилось: доску и копию в таблице пора обновить."""
        self._board_dirty.set()
        self._sheet_dirty.set()

    def _warn(self, text: str) -> None:
        log.warning(text)
        if text not in self.info.warnings:
            self.info.warnings.append(text)

    async def startup(self) -> None:
        me = await self.bot.get_me()
        self.bot_id = me.id
        self.info.started_at = self.tracker.now()
        log.info("Бот @%s запущен", me.username)
        if self.settings.chat_id is None:
            self._warn("chat_id не задан в config.yaml: бот отвечает только на /id")
        await self.check_rights(me.can_read_all_group_messages)
        await self.notice_downtime()

    async def check_rights(self, reads_all: bool | None = None) -> None:
        chat_id = self.settings.chat_id
        if chat_id is None or self.bot_id is None:
            return
        self.info.warnings = [w for w in self.info.warnings if not w.startswith("Права бота")]
        try:
            member = await self.bot.get_chat_member(chat_id, self.bot_id)
        except TelegramAPIError as error:
            self._warn(f"Права бота: не удалось проверить чат {chat_id}: {error}")
            return
        if not isinstance(member, ChatMemberAdministrator | ChatMemberOwner):
            note = "" if reads_all else " и без этого он не видит сообщения без /команд"
            self._warn(
                f"Права бота: он не администратор чата — не сможет закреплять доску{note}. "
                "Сделайте бота администратором."
            )
        elif isinstance(member, ChatMemberAdministrator) and not member.can_pin_messages:
            self._warn("Права бота: нет права закреплять сообщения — доску закрепить не выйдет")

    async def notice_downtime(self) -> None:
        heartbeat = self.tracker.state.get_meta("heartbeat")
        if not heartbeat or self.settings.chat_id is None:
            return
        gap = self.tracker.now() - datetime.fromisoformat(heartbeat)
        if gap > timedelta(hours=DOWNTIME_HOURS):
            hours = int(gap.total_seconds() // 3600)
            await self.sender.send(
                Reply(
                    f"⚠️ Меня не было около {hours} ч, и я мог пропустить сообщения "
                    "(Telegram хранит их для бота сутки). Сверьте доску — /board — и поправьте "
                    "этапы, если что-то не так.",
                    kind="notice",
                )
            )

    # --- доска ------------------------------------------------------------------------------
    async def publish_board(
        self, chat_id: int, thread_id: int | None, reply_to: int | None
    ) -> None:
        text = build_board(self.tracker, self.tracker.now(), self.parsed_kp)
        old = self.tracker.state.get_meta("board_message_id")
        if old and await self.sender.edit(chat_id, int(old), text):
            await self.sender.send(
                Reply("📌 Доска обновлена."),
                reply_to=reply_to,
                chat_id=chat_id,
                thread_id=thread_id,
            )
            return
        sent = await self.sender.send(
            Reply(text, kind="board"), chat_id=chat_id, thread_id=thread_id
        )
        if sent is None:
            return
        self.tracker.state.set_meta("board_message_id", str(sent.message_id))
        try:
            await self.bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except TelegramAPIError as error:
            log.warning("Не удалось закрепить доску: %s", error)
            await self.sender.send(
                Reply(
                    "Не смог закрепить доску: дайте боту право закреплять сообщения "
                    "(администратор → «Закрепление сообщений»)."
                ),
                chat_id=chat_id,
                thread_id=thread_id,
            )

    async def refresh_board(self) -> None:
        board_id = self.tracker.state.get_meta("board_message_id")
        chat_id = self.settings.chat_id
        if not board_id or chat_id is None:
            return
        text = build_board(self.tracker, self.tracker.now(), self.parsed_kp)
        if await self.sender.edit(chat_id, int(board_id), text):
            self._board_failures = 0
            return
        self._board_failures += 1
        if self._board_failures >= 3:  # доску, видимо, удалили; новая — по /board
            log.warning("Доска недоступна, забываю её")
            self.tracker.state.set_meta("board_message_id", None)
            self._board_failures = 0

    # --- КП -------------------------------------------------------------------------------------
    async def sync_kp_once(self) -> None:
        if self.google is None or not self.env.kp_spreadsheet_id:
            return
        try:
            data = await asyncio.to_thread(self.google.export_xlsx, self.env.kp_spreadsheet_id)
            await self.apply_kp_data(data)
        except GoogleError as error:
            await self._kp_failed(str(error))
        except Exception as error:
            log.exception("Сбой при чтении КП")
            await self._kp_failed(f"{type(error).__name__}: {error}")

    async def _kp_failed(self, reason: str) -> None:
        self._kp_failures += 1
        self.info.last_kp_error = reason
        log.error("КП не прочитано (%s подряд): %s", self._kp_failures, reason)
        if self._kp_failures < KP_FAILURES_BEFORE_ALERT:
            return
        today = self.tracker.today().isoformat()
        if self.tracker.state.get_meta("kp_alert_day") == today:
            return
        self.tracker.state.set_meta("kp_alert_day", today)
        await self.sender.send(
            Reply(
                f"⚠️ Не могу прочитать КП уже {self._kp_failures} раз подряд: {escape(reason)}\n"
                "Трекер работает по последним известным данным.",
                kind="notice",
            )
        )

    async def apply_kp_data(self, data: bytes) -> None:
        now = self.tracker.now()
        digest = hashlib.sha256(data).hexdigest()
        self._kp_failures = 0
        self.info.last_kp_error = None
        if digest == self._kp_hash:
            self.info.last_kp_sync = now
            return
        today = now.date()
        parsed = await asyncio.to_thread(
            parse_kp,
            data,
            since=today - timedelta(days=self.settings.history_days),
            until=today + timedelta(days=self.settings.horizon_days + 45),
            status_stages=self.settings.status_stages(),
        )
        report = sync_kp(self.tracker, parsed.slots, status_stages=self.settings.status_stages())
        self.parsed_kp = parsed
        self._kp_hash = digest

        baseline = self.tracker.state.get_meta("comments_baseline") is None
        record_comments(
            self.tracker,
            [
                ExternalComment(
                    id=f"kp:{c.id}", source="kp", author=c.author, text=c.text, created=c.created,
                    resolved=c.resolved, day=c.day, slot=c.slot,
                )
                for c in parsed.comments
            ],
            report.slot_to_post,
            {},
            baseline=baseline,
        )  # fmt: skip
        self.tracker.state.set_meta("comments_baseline", "1")

        self.info.kp_posts = self.tracker.count_posts("kp")
        self.info.last_kp_sync = now
        self.info.warnings = [w for w in self.info.warnings if w.startswith("Права бота")]
        self.info.warnings.extend(parsed.warnings)
        await self._announce_warnings(parsed.warnings)
        for reply in sync_messages(self.tracker, report):
            await self.sender.send(reply)
        await self.notify_comments()
        self.mark_dirty()

    async def _announce_warnings(self, warnings: list[str]) -> None:
        signature = (
            hashlib.sha1("\n".join(sorted(warnings)).encode()).hexdigest() if warnings else ""
        )
        if signature == (self.tracker.state.get_meta("kp_warn_sig") or ""):
            return
        self.tracker.state.set_meta("kp_warn_sig", signature or None)
        if warnings:
            body = "\n".join(f"• {escape(w)}" for w in warnings[:6])
            await self.sender.send(Reply(f"⚠️ С КП что-то не так:\n{body}", kind="notice"))

    async def notify_comments(self) -> None:
        reply = comment_reply(self.tracker, self.tracker.now())
        if reply is None:
            return
        if await self.sender.send(reply) is not None:
            mark_comments_notified(self.tracker, reply.meta["comment_ids"])

    async def poll_docs_once(self) -> None:
        if self.google is None:
            return
        today = self.tracker.today()
        posts = [p for p in self.tracker.open_posts(today, back_days=1, ahead_days=21) if p.doc_id]
        state = self.tracker.state
        for post in posts:
            doc_id = post.doc_id
            if doc_id is None or state.get_meta(f"doc_denied:{doc_id}") == today.isoformat():
                continue
            try:
                comments = await asyncio.to_thread(self.google.doc_comments, doc_id)
            except GoogleError as error:
                if error.status in (403, 404):
                    state.set_meta(f"doc_denied:{doc_id}", today.isoformat())
                    self._warn(
                        f"Нет доступа к доку поста №{post.id}: расшарьте его на сервисный аккаунт"
                    )
                    continue
                log.warning("Комментарии дока поста №%s не прочитаны: %s", post.id, error)
                continue
            baseline = state.get_meta(f"doc_seen:{doc_id}") is None
            record_comments(self.tracker, comments, {}, {doc_id: post.id}, baseline=baseline)
            state.set_meta(f"doc_seen:{doc_id}", "1")
        await self.notify_comments()

    # --- напоминания --------------------------------------------------------------------------
    def _awaiting_first_sync(self, now: datetime) -> bool:
        """После запуска даём боту до 5 минут на первое чтение КП, если оно подключено."""
        if not self.info.google_configured or self.info.last_kp_sync is not None:
            return False
        started = self.info.started_at
        return started is not None and now - started < timedelta(minutes=FIRST_SYNC_WAIT_MINUTES)

    async def tick(self) -> None:
        now = self.tracker.now()
        self.tracker.state.set_meta("heartbeat", now.isoformat())
        if self.settings.chat_id is None:
            return
        if self.tracker.state.get_meta("first_run") is None:
            self.tracker.state.set_meta("first_run", now.date().isoformat())
        if self._awaiting_first_sync(now):
            return  # иначе утреннее саммари могло бы уйти раньше, чем бот прочитал КП
        for rule, reply in due_rules(self.tracker, now, self.parsed_kp):
            if reply is not None and reply.text:
                if await self.sender.send(reply) is None:
                    continue  # не отправилось — попробуем на следующем такте, пока не вышел срок
                ids = reply.meta.get("comment_ids")
                if ids:
                    mark_comments_notified(self.tracker, ids)
            mark_done(self.tracker, rule, now, reply)

    # --- копия трекера и календарь ---------------------------------------------------------------
    async def push_sheet_once(self) -> None:
        if self.google is None or not self.env.tracker_spreadsheet_id:
            return
        now = self.tracker.now()
        try:
            rows = rows_for(self.tracker, now)
            await asyncio.to_thread(push_rows, self.google, self.env.tracker_spreadsheet_id, rows)
        except Exception as error:
            log.exception("Копия трекера не записана")
            self.info.last_tracker_copy_error = str(error)
            return
        self.info.last_tracker_copy = now
        self.info.last_tracker_copy_error = None

    async def refresh_calendar_once(self) -> None:
        year = self.tracker.today().year
        await asyncio.to_thread(
            refresh_calendar,
            self.tracker.calendar,
            [year, year + 1],
            self.env.data_dir / "calendar_cache.json",
        )

    # --- запуск -----------------------------------------------------------------------------------
    async def _loop(self, name: str, action: Any, interval: float, delay: float = 0) -> None:
        await asyncio.sleep(delay)
        while True:
            try:
                await action()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Сбой в цикле «%s»", name)
            await asyncio.sleep(interval)

    async def _board_loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._board_dirty.wait(), timeout=BOARD_REFRESH_SECONDS)
            await asyncio.sleep(3)  # накопить несколько изменений подряд
            self._board_dirty.clear()
            try:
                await self.refresh_board()
            except Exception:
                log.exception("Не удалось обновить доску")

    async def _sheet_loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._sheet_dirty.wait(), timeout=SHEET_REFRESH_SECONDS)
            self._sheet_dirty.clear()
            await self.push_sheet_once()
            await asyncio.sleep(SHEET_MIN_INTERVAL)

    async def run(self) -> None:
        await self.startup()
        settings = self.settings
        self._tasks = [
            asyncio.create_task(self._loop("напоминания", self.tick, TICK_SECONDS, 5)),
            asyncio.create_task(self._loop("КП", self.sync_kp_once, settings.poll_kp_seconds, 3)),
            asyncio.create_task(
                self._loop("доки", self.poll_docs_once, settings.poll_docs_seconds, 60)
            ),
            asyncio.create_task(
                self._loop("календарь", self.refresh_calendar_once, CALENDAR_REFRESH_SECONDS, 10)
            ),
            asyncio.create_task(self._board_loop()),
            asyncio.create_task(self._sheet_loop()),
        ]
        try:
            await self.dp.start_polling(self.bot, allowed_updates=ALLOWED_UPDATES)
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await self.bot.session.close()


def build_app(env: Env, settings: Settings) -> App:
    """Собирает бота из настроек и переменных окружения."""
    if not env.bot_token:
        raise SystemExit("Не задан BOT_TOKEN: возьмите токен у @BotFather и положите в .env")
    calendar = build_calendar(
        env.data_dir / "calendar_cache.json", settings.extra_days_off, settings.extra_workdays
    )
    tracker = Tracker(Database(env.db_path), settings, calendar)
    google = None
    if env.google_credentials and env.google_credentials.exists():
        google = GoogleClient.from_service_account(env.google_credentials)
    else:
        log.warning("Ключ сервисного аккаунта не найден: КП читаться не будет")
    bot = Bot(env.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return App(env, settings, bot, tracker, google)
