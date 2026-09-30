"""Обработчики Telegram: сообщения команды, команды бота, кнопки."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from norm_tasker.ai.assistant import ask_text
from norm_tasker.ai.gemini import AiError
from norm_tasker.bot import views
from norm_tasker.bot.telegram import message_link
from norm_tasker.chat.actions import handle_callback, handle_message
from norm_tasker.chat.asks import addressed_to, classify_ask
from norm_tasker.config import Role
from norm_tasker.digest.board import build_board
from norm_tasker.reply import Reply
from norm_tasker.tg.commands import parse_command
from norm_tasker.tg.errors import TelegramError
from norm_tasker.tg.types import CallbackQuery, ChatMemberUpdated, Message, Update, User
from norm_tasker.tracker.models import Actor

if TYPE_CHECKING:
    from norm_tasker.bot.app import App

log = logging.getLogger(__name__)

PRIVATE_START = (
    "Привет! Я работаю в рабочем чате команды и пишу там, а здесь меня трогать не нужно.\n"
    "Чтобы настроить меня, отправьте /id в рабочем чате и в личке — я покажу идентификаторы."
)

CommandHandler = Callable[[Message, str], Awaitable[None]]


class Handlers:
    def __init__(self, app: App) -> None:
        self.app = app
        # Команды рабочего чата. /id работает везде, /start и /help в личке — отдельно.
        self.commands: dict[str, CommandHandler] = {
            "help": self.cmd_help,
            "start": self.cmd_help,
            "today": self.cmd_today,
            "week": self.cmd_week,
            "my": self.cmd_my,
            "free": self.cmd_free,
            "design": self.cmd_design,
            "kp": self.cmd_kp,
            "post": self.cmd_post,
            "add": self.cmd_add,
            "board": self.cmd_board,
            "undo": self.cmd_undo,
            "inventory": self.cmd_inventory,
            "status": self.cmd_status,
        }

    # --- вспомогательное -----------------------------------------------------------------
    @property
    def tracker(self):
        return self.app.tracker

    @property
    def sender(self):
        return self.app.sender

    def in_work_chat(self, chat_id: int) -> bool:
        return self.app.settings.chat_id is not None and chat_id == self.app.settings.chat_id

    def identify(self, user: User | None) -> Actor | None:
        if user is None or user.is_bot:
            return None
        return self.tracker.team.identify(
            user.id, user.username, user.full_name, self.tracker.now()
        )

    @staticmethod
    def thread_of(message: Message) -> int | None:
        return message.message_thread_id if message.is_topic_message else None

    async def respond(self, message: Message, reply: Reply) -> None:
        """Ответ на сообщение: реакция, текст или и то и другое."""
        if reply.react:
            reacted = await self.sender.react(message.chat.id, message.message_id)
            if not reacted and reply.text is None:
                reply = Reply("👍 Записал.", kind="reply")
        if reply.text:
            await self.sender.send(
                reply,
                reply_to=message.message_id,
                chat_id=message.chat.id,
                thread_id=self.thread_of(message),
            )

    # --- разбор апдейтов ---------------------------------------------------------------------
    async def on_update(self, update: Update) -> None:
        if update.message is not None:
            await self.on_message(update.message)
        elif update.callback_query is not None:
            await self.on_callback(update.callback_query)
        elif update.my_chat_member is not None:
            await self.on_my_chat_member(update.my_chat_member)

    async def on_message(self, message: Message) -> None:
        text = message.text or message.caption
        command = parse_command(text, self.app.bot_username)
        if command is not None:
            if command.name == "id":
                await self.cmd_id(message)
                return
            if command.name in ("start", "help") and message.chat.type == "private":
                await self.cmd_start_private(message)
                return
        if not self.in_work_chat(message.chat.id):
            return
        handler = self.commands.get(command.name) if command is not None else None
        if handler is not None and command is not None:
            await handler(message, command.args)
        elif text:
            await self.on_text(message)

    # --- команды ----------------------------------------------------------------------------
    async def cmd_id(self, message: Message) -> None:
        user = message.from_user
        actor = self.identify(user)
        text = views.id_text(
            message.chat.id,
            self.thread_of(message),
            user.id if user else None,
            user.username if user else None,
            actor,
        )
        await self.sender.send(
            Reply(text), reply_to=message.message_id, chat_id=message.chat.id,
            thread_id=self.thread_of(message),
        )  # fmt: skip

    async def cmd_start_private(self, message: Message) -> None:
        await self.sender.send(Reply(PRIVATE_START), chat_id=message.chat.id, thread_id=None)

    async def cmd_help(self, message: Message, args: str = "") -> None:
        await self.respond(message, views.help_reply())

    async def cmd_today(self, message: Message, args: str = "") -> None:
        await self.respond(message, views.today_view(self.tracker, self.app.parsed_kp))

    async def cmd_week(self, message: Message, args: str = "") -> None:
        await self.respond(message, views.week_view(self.tracker, self.app.parsed_kp))

    async def cmd_my(self, message: Message, args: str = "") -> None:
        actor = self.identify(message.from_user)
        if actor is not None:
            await self.respond(message, views.my_view(self.tracker, actor))

    async def cmd_free(self, message: Message, args: str = "") -> None:
        await self.respond(message, views.free_view(self.tracker))

    async def cmd_design(self, message: Message, args: str = "") -> None:
        await self.respond(message, views.design_view(self.tracker))

    async def cmd_kp(self, message: Message, args: str = "") -> None:
        await self.respond(message, views.kp_view(self.tracker, self.app.parsed_kp))

    async def cmd_post(self, message: Message, args: str = "") -> None:
        query = args.strip()
        if not query:
            await self.respond(message, Reply("Укажите пост: /post 12 или /post 16.10"))
            return
        posts = views.find_posts(self.tracker, query)
        if not posts:
            await self.respond(message, Reply("Не нашёл такой пост. Посты недели — /week."))
            return
        text = "\n\n———\n\n".join(views.post_view(self.tracker, p) for p in posts)
        await self.respond(message, Reply(text, post_ids=[p.id for p in posts], kind="confirm"))

    async def cmd_add(self, message: Message, args: str = "") -> None:
        actor = self.identify(message.from_user)
        if actor is None or actor.role == Role.BOSS:
            return
        await self.respond(message, views.add_post_from_text(self.tracker, actor, args))
        self.app.mark_dirty()

    async def cmd_undo(self, message: Message, args: str = "") -> None:
        actor = self.identify(message.from_user)
        if actor is None:
            return
        entry = self.tracker.last_undoable(actor)
        if entry is None:
            await self.respond(
                message, Reply("Нечего отменять: за последние сутки от вас действий нет.")
            )
            return
        result = handle_callback(self.tracker, f"undo:{entry.id}", actor)
        await self.respond(
            message, Reply(result.edit_text or result.alert or "Не получилось отменить.")
        )
        self.app.mark_dirty()

    async def cmd_inventory(self, message: Message, args: str = "") -> None:
        actor = self.identify(message.from_user)
        if actor is None or actor.role != Role.RESPONSIBLE:
            await self.respond(message, Reply("Инвентаризацию запускает ответственный за проект."))
            return
        await self.respond(message, views.inventory_view(self.tracker))

    async def cmd_status(self, message: Message, args: str = "") -> None:
        await self.respond(
            message,
            views.status_view(self.tracker, self.app.info, self.app.settings.chat_id is not None),
        )

    async def cmd_board(self, message: Message, args: str = "") -> None:
        await self.app.publish_board(message.chat.id, self.thread_of(message), message.message_id)

    # --- сообщения команды ---------------------------------------------------------------------
    async def on_text(self, message: Message) -> None:
        actor = self.identify(message.from_user)
        text = message.text or message.caption or ""
        if actor is not None and await self.on_report(message, actor, text):
            return
        if addressed_to(text, self.app.bot_username):
            await self.on_mention(message, actor, text)

    async def on_mention(self, message: Message, actor: Actor | None, text: str) -> None:
        """Бота позвали по имени, а фраза не похожа на отчёт о посте: отвечаем, а не молчим."""
        question = ask_text(text, self.app.bot_username)
        if question and self.app.ai_allowed():
            self.app.spawn(self.answer_with_ai(message, actor, question, text))
            return
        await self.answer_by_keywords(message, actor, text)

    async def answer_with_ai(
        self, message: Message, actor: Actor | None, question: str, text: str
    ) -> None:
        """Вопрос по имени бота уходит в Gemini вместе со сводкой по постам; при сбое — команды."""
        app = self.app
        assert app.ai is not None
        with contextlib.suppress(TelegramError):
            await app.bot.send_chat_action(message.chat.id, "typing", self.thread_of(message))
        try:
            answer = await app.ai.answer(question, actor, app.parsed_kp)
        except AiError as error:
            app.ai_failed(error)
        except Exception as error:
            log.exception("Ответ ИИ не удалось подготовить")
            app.ai_failed(error)
        else:
            app.ai_succeeded()
            await self.respond(message, Reply(answer))
            return
        await self.answer_by_keywords(message, actor, text)

    async def answer_by_keywords(self, message: Message, actor: Actor | None, text: str) -> None:
        """Без ИИ: узнаём частые слова («задачи», «сегодня», «мои») или подсказываем команды."""
        name = classify_ask(text, self.app.bot_username)
        handler = self.commands.get(name) if name else None
        if handler is not None and (name != "my" or actor is not None):
            await handler(message, "")
            return
        await self.respond(message, views.unsure_reply())

    async def on_report(self, message: Message, actor: Actor, text: str) -> bool:
        """Отчёт команды о постах. False — бот ничего не понял или не нашёл, что ответить."""
        reply_post_ids = None
        replied = message.reply_to_message
        if (
            replied is not None
            and replied.from_user is not None
            and replied.from_user.id == self.app.bot_id
        ):
            reply_post_ids = self.tracker.state.message_posts(message.chat.id, replied.message_id)
        try:
            reply = handle_message(
                self.tracker,
                text,
                actor,
                reply_post_ids=reply_post_ids,
                msg_link=message_link(message.chat.id, message.message_id),
                parsed_kp=self.app.parsed_kp,
            )
        except Exception:
            log.exception("Не удалось обработать сообщение %s", message.message_id)
            return True  # сбой уже в журнале; «не понял» тут было бы неправдой
        if reply is None or reply.is_empty():
            return False
        await self.respond(message, reply)
        self.app.mark_dirty()
        return True

    # --- кнопки --------------------------------------------------------------------------------
    async def answer(
        self, query: CallbackQuery, text: str | None, show_alert: bool = False
    ) -> None:
        try:
            await self.app.bot.answer_callback_query(query.id, text, show_alert=show_alert)
        except TelegramError:
            log.info("Уведомление о нажатии не доставлено", exc_info=True)

    async def on_callback(self, query: CallbackQuery) -> None:
        message = query.message
        if message is None or not self.in_work_chat(message.chat.id):
            return
        actor = self.identify(query.from_user)
        if actor is None:
            await self.answer(query, "Вас нет в списке команды", show_alert=True)
            return
        try:
            result = handle_callback(
                self.tracker, query.data or "", actor, parsed_kp=self.app.parsed_kp
            )
        except Exception:
            log.exception("Не удалось обработать кнопку %s", query.data)
            await self.answer(query, "Что-то пошло не так, попробуйте ещё раз", show_alert=True)
            return
        await self.answer(query, result.alert or None, show_alert=bool(result.alert))
        if result.edit_text is not None:
            await self.sender.edit(message.chat.id, message.message_id, result.edit_text)
        elif result.remove_buttons:
            await self.sender.remove_buttons(message.chat.id, message.message_id)
        if result.reply is not None and not result.reply.is_empty():
            result.reply.react = False  # реакция на сообщение бота с кнопкой бессмысленна
            await self.respond(message, result.reply)
        self.app.mark_dirty()

    async def on_my_chat_member(self, event: ChatMemberUpdated) -> None:
        log.info(
            "Статус бота в чате %s изменился: %s → %s",
            event.chat.id,
            event.old_chat_member.status,
            event.new_chat_member.status,
        )
        if event.chat.id == self.app.settings.chat_id:
            await self.app.check_rights()

    def board_text(self) -> str:
        return build_board(self.tracker, self.tracker.now(), self.app.parsed_kp)
