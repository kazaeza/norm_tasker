import asyncio
import dataclasses
import threading
from datetime import date, datetime, timedelta

import pytest

from ai_fixture import KEY, FakeTransport, answer, error_body, verdict
from kp_fixture import build_kp, standard_kp
from norm_tasker.ai.assistant import Assistant
from norm_tasker.ai.gemini import GeminiClient
from norm_tasker.bot import views
from norm_tasker.bot.app import KP_FAILURES_BEFORE_ALERT, App
from norm_tasker.config import Env
from norm_tasker.google.client import GoogleError
from norm_tasker.tg.types import Chat, ChatMember, ChatMemberUpdated, Message, Update
from norm_tasker.tracker.db import Database
from norm_tasker.tracker.models import ExternalComment
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import Stage
from tg_fixture import (
    BOT_ID,
    WORK_CHAT,
    FakeApi,
    FakeGoogle,
    bot_message,
    callback_update,
    make_user,
    text_update,
)

ALPHA = {"user_id": 101, "username": "cw_alpha", "name": "Альфа"}
BETA = {"user_id": 102, "username": "cw_beta", "name": "Бета"}
LEAD = {"user_id": 103, "username": "lead_user", "name": "Ответственный"}
FRIDAY = date(2026, 10, 2)


@pytest.fixture
def session():
    return FakeApi()


@pytest.fixture
def app(tmp_path, settings, calendar, clock, session):
    tracker = Tracker(Database(":memory:"), settings, calendar, clock)
    env = Env(
        bot_token="42:TEST",
        data_dir=tmp_path,
        config_path=tmp_path / "config.yaml",
        google_credentials=None,
        kp_spreadsheet_id="KP",
        tracker_spreadsheet_id="TRK",
    )
    tracker.state.set_meta("first_run", "2026-09-01")  # мягкий старт закончился
    return App(env, settings, session, tracker, FakeGoogle(build_kp(standard_kp())))


@pytest.fixture
async def ready(app, session):
    """Бот запущен, КП прочитано, журнал вызовов пуст."""
    await app.startup()
    await app.apply_kp_data(app.google.xlsx)
    session.clear()
    return app


async def feed(app, update):
    await app.handlers.on_update(update)


async def say(app, text, who, **kwargs):
    await feed(app, text_update(text, **who, **kwargs))


def friday_post(app):
    return next(p for p in app.tracker.posts_on(FRIDAY))


def last_button(session, index=0, row=0):
    return session.named("SendMessage")[-1]["reply_markup"]["inline_keyboard"][row][index]


# --- КП ------------------------------------------------------------------------------------


async def test_kp_sync_creates_posts_and_reports_template_warning(app, session):
    await app.startup()
    session.clear()
    await app.apply_kp_data(app.google.xlsx)
    assert app.tracker.count_posts("kp") >= 8
    assert any("Странный статус" in text for text in session.sent_texts())
    assert app.info.kp_posts == app.tracker.count_posts("kp")
    # Те же данные — молчание.
    session.clear()
    await app.apply_kp_data(app.google.xlsx)
    assert session.calls == []


async def test_kp_read_failures_alert_once_a_day(app, session):
    await app.startup()
    session.clear()

    def fail(file_id):
        raise GoogleError(404, "файл не найден. нет доступа")

    app.google.export_xlsx = fail
    for _ in range(KP_FAILURES_BEFORE_ALERT + 2):
        await app.sync_kp_once()
    alerts = [t for t in session.sent_texts() if "Не могу прочитать КП" in t]
    assert len(alerts) == 1 and "нет доступа" in alerts[0]
    assert "нет доступа" in app.info.last_kp_error


async def test_kp_changes_are_announced(ready, session, alpha):
    app = ready
    post = friday_post(app)
    app.tracker.claim(post.id, app.tracker.team.identify(101, "cw_alpha", "Альфа"))
    changed = standard_kp()
    # В КП тему пятничного поста заменили совсем.
    changed["Октябрь 2026"][0].days[4].topic = "Совсем другая тема про свидания"
    await app.apply_kp_data(build_kp(changed))
    texts = "\n".join(session.sent_texts())
    assert "изменилась тема поста" in texts and "Совсем другая тема" in texts
    assert "@cw_alpha" in texts


# --- сообщения команды ---------------------------------------------------------------------


async def test_claim_and_undo_by_button(ready, session):
    app = ready
    await say(app, "беру пост на пятницу", ALPHA)
    sent = session.named("SendMessage")[-1]
    assert "Копирайтер Альфа" in sent["text"] and "пт 02.10" in sent["text"]
    assert sent["reply_parameters"]["allow_sending_without_reply"] is True
    assert friday_post(app).assignee_username == "cw_alpha"

    undo = last_button(session)["callback_data"]
    assert undo.startswith("undo:")
    session.clear()
    await feed(app, callback_update(undo, message_id=session._next_id, **ALPHA))
    assert not friday_post(app).assigned
    assert session.named("AnswerCallbackQuery")
    assert "Отменено" in session.named("EditMessageText")[0]["text"]


async def test_named_post_gets_a_reaction_only(ready, session):
    app = ready
    await say(app, "беру пост на пятницу", ALPHA)
    session.clear()
    await say(app, "текст на пятницу готов, отправил клиенту", ALPHA)
    assert session.named("SetMessageReaction")[0]["reaction"][0]["emoji"] == "👍"
    assert session.named("SendMessage") == []
    assert friday_post(app).stage == Stage.TEXT_SHOWN


async def test_reaction_fallback_when_reactions_are_off(ready, session):
    app = ready
    await say(app, "беру пост на пятницу", ALPHA)
    session.clear()
    session.reject_reactions = True
    await say(app, "текст на пятницу готов", ALPHA)
    assert session.last_text() == "👍 Записал."


async def test_reply_to_bot_message_selects_the_post(ready, session):
    app = ready
    post = friday_post(app)
    app.tracker.state.remember_message(WORK_CHAT, 777, "free", [post.id], app.tracker.now())
    await say(app, "беру", BETA, reply_to=bot_message(777))
    assert app.tracker.get(post.id).assignee_username == "cw_beta"
    assert session.named("SendMessage")[-1]["text"].startswith("✍️ Копирайтер Бета")


async def test_ambiguity_is_asked_with_buttons(ready, session):
    app = ready
    await say(app, "беру", ALPHA)  # свободных постов много
    sent = session.named("SendMessage")[-1]
    assert sent["text"] == "Какой пост имеется в виду?"
    rows = sent["reply_markup"]["inline_keyboard"]
    assert rows[-1][0]["text"] == "Никакой"
    pick = rows[0][0]["callback_data"]
    assert pick.startswith("pick:")
    await feed(app, callback_update(pick, message_id=session._next_id, **ALPHA))
    assert any(
        p.assignee_username == "cw_alpha" for p in app.tracker.open_posts(app.tracker.today())
    )


async def test_chatter_and_strangers_are_ignored(ready, session):
    app = ready
    await say(app, "всем привет, как выходные?", ALPHA)
    await say(app, "беру пост на пятницу", ALPHA, chat_id=-1009999999999)
    app.tracker.settings.unknown_role = None
    await say(app, "беру пост на пятницу", {"user_id": 999, "username": "who", "name": "Кто-то"})
    assert session.named("SendMessage") == []
    assert not friday_post(app).assigned


async def test_boss_messages_are_ignored(ready, session):
    await say(ready, "беру пост на пятницу", {"user_id": 104, "username": "boss_user", "name": "Т"})
    assert session.named("SendMessage") == []


async def test_button_from_stranger_is_refused(ready, session):
    app = ready
    app.tracker.settings.unknown_role = None
    post = friday_post(app)
    await feed(
        app,
        callback_update(
            f"claim:{post.id}", message_id=1, user_id=999, username="who", name="Кто-то"
        ),
    )
    assert session.named("AnswerCallbackQuery")[0]["show_alert"] is True
    assert not app.tracker.get(post.id).assigned


# --- команды ---------------------------------------------------------------------------------


async def test_commands(ready, session):
    app = ready
    for command, marker in [
        ("/help", "Команды:"),
        ("/today", "☀️"),
        ("/week", "📌 Доска"),
        ("/free", "Без ответственного"),
        ("/kp", "📅 КП на"),
        ("/design", "Дизайн"),
        ("/my", "За вами ничего нет"),
        ("/status", "Версия"),
    ]:
        session.clear()
        await say(app, command, ALPHA)
        assert marker in session.last_text(), command


async def test_post_and_add_commands(ready, session):
    app = ready
    post = friday_post(app)
    await say(app, f"/post {post.id}", ALPHA)
    assert "Сроки:" in session.last_text() and "Тема про пятницу" in session.last_text()
    await say(app, "/post 02.10", ALPHA)
    assert f"№{post.id}" in session.last_text()
    await say(app, "/post 31.12", ALPHA)
    assert "Не нашёл такой пост" in session.last_text()
    await say(app, "/add 14.10 срочный пост про акцию", ALPHA)
    assert (
        "Добавлен пост" in session.last_text() and "срочный пост про акцию" in session.last_text()
    )
    assert app.tracker.posts_on(date(2026, 10, 14))[0].source == "chat"


async def test_undo_command(ready, session):
    app = ready
    await say(app, "/undo", ALPHA)
    assert "Нечего отменять" in session.last_text()
    await say(app, "беру пост на пятницу", ALPHA)
    await say(app, "/undo", ALPHA)
    assert "Отменено" in session.last_text() and not friday_post(app).assigned


async def test_inventory_is_for_the_responsible_only(ready, session):
    app = ready
    await say(app, "/inventory", ALPHA)
    assert "запускает ответственный" in session.last_text()
    await say(app, "/inventory", LEAD)
    assert "сверим трекер" in session.last_text()


async def test_id_works_anywhere_and_start_in_private(ready, session):
    app = ready
    await say(app, "/id", ALPHA, chat_id=-1005555)
    text = session.last_text()
    assert "-1005555" in text and "Копирайтер Альфа" in text
    await say(app, "/start", ALPHA, chat_id=101, chat_type="private")
    assert "Чтобы настроить меня" in session.last_text()
    session.clear()
    await say(app, "/today", ALPHA, chat_id=-1005555)  # чужой чат: команды не работают
    assert session.named("SendMessage") == []


async def test_commands_addressed_to_another_bot_are_not_ours(ready, session):
    app = ready
    await say(app, "/today@norm_test_bot", ALPHA)  # имя нашего бота — команда наша
    assert "☀️" in session.last_text()
    session.clear()
    await say(app, "/today@someone_else_bot", ALPHA)
    await say(app, "/help@someone_else_bot", ALPHA, chat_id=101, chat_type="private")
    await say(app, "/unknown_command", ALPHA)
    assert session.named("SendMessage") == []


async def test_mention_with_a_question_gets_an_answer(ready, session):
    app = ready
    for text, marker in [
        ("@norm_test_bot чё там по задачам", "📌 Доска"),  # как /week
        ("@norm_test_bot чё по задачам?", "📌 Доска"),
        ("@Norm_Test_Bot что горит сегодня", "☀️"),
        ("@norm_test_bot какие свободные посты", "Без ответственного"),
        ("@norm_test_bot что по кп", "📅 КП на"),
        ("@norm_test_bot мои посты", "За вами ничего нет"),
    ]:
        session.clear()
        await say(app, text, ALPHA)
        assert marker in session.last_text(), text


async def test_mention_without_known_words_gets_a_hint(ready, session):
    app = ready
    for text in ("@norm_test_bot", "@norm_test_bot как погода?"):
        session.clear()
        await say(app, text, ALPHA)
        reply = session.last_text()
        assert "Не понял" in reply and "/week" in reply and "/help" in reply, text
        assert session.named("SendMessage")[-1]["reply_parameters"]["message_id"]


async def test_without_our_name_the_bot_stays_silent(ready, session):
    app = ready
    await say(app, "чё там по задачам", ALPHA)
    await say(app, "@someone_else_bot чё там по задачам", ALPHA)
    await say(app, "@norm_test_bot чё там по задачам", ALPHA, chat_id=-1005555)  # чужой чат
    assert session.named("SendMessage") == []


async def test_report_with_a_mention_is_still_a_report(ready, session):
    app = ready
    await say(app, "@norm_test_bot беру пост на пятницу", ALPHA)
    assert friday_post(app).assignee_username == "cw_alpha"
    assert "Не понял" not in session.last_text()


# --- ответы на сообщения бота ---------------------------------------------------------------


def linked_bot_message(app, post, message_id=777):
    """Сообщение бота о посте: на него отвечают, а бот помнит, о каком посте оно."""
    now = app.tracker.now()
    app.tracker.state.remember_message(WORK_CHAT, message_id, "confirm", [post.id], now)
    return bot_message(message_id)


async def test_reply_to_the_bot_is_a_call_to_the_bot(ready, session):
    app = ready
    for text, marker in [
        ("что горит сегодня", "☀️"),
        ("чё там по задачам?", "📌 Доска"),
        ("а какие свободные посты?", "Без ответственного"),
    ]:
        session.clear()
        await say(app, text, ALPHA, reply_to=bot_message(777))
        assert marker in session.last_text(), text
    session.clear()
    await say(app, "как погода?", ALPHA, reply_to=bot_message(777))
    assert "Не понял" in session.last_text()
    assert session.named("SendMessage")[-1]["reply_parameters"]["message_id"]


async def test_reply_report_is_still_a_report_and_thanks_get_no_answer(ready, session):
    app = ready
    post = friday_post(app)
    await say(app, "беру", BETA, reply_to=linked_bot_message(app, post))
    assert app.tracker.get(post.id).assignee_username == "cw_beta"
    session.clear()
    for text in ("спасибо", "Спасибо большое!", "ок", "👍", "понял, спасибо"):
        await say(app, text, ALPHA, reply_to=bot_message(777))
    assert session.named("SendMessage") == []


async def test_only_replies_to_our_own_messages_count(ready, session):
    app = ready
    colleague = Message(
        message_id=888,
        chat=Chat(id=WORK_CHAT, type="supergroup"),
        from_user=make_user(102, "cw_beta", "Бета"),
        text="кто взял пост?",
    )
    await say(app, "чё там по задачам", ALPHA, reply_to=colleague)
    # Ответ другого бота на наше сообщение: бот с ботами не переписывается.
    update = text_update(
        "чё там по задачам", user_id=555, username="other_bot", reply_to=bot_message(777)
    )
    update.message.from_user.is_bot = True
    await feed(app, update)
    assert session.named("SendMessage") == []


async def test_stranger_gets_read_only_answers(ready, session):
    app = ready
    app.settings.unknown_role = None  # людей вне списка команды бот не считает участниками
    stranger = {"user_id": 999, "username": "stranger_x", "name": "Гость"}
    await say(app, "@norm_test_bot что горит сегодня", stranger)
    assert "☀️" in session.last_text()
    session.clear()
    await say(app, "@norm_test_bot мои посты", stranger)  # «мои» у того, кого нет в команде
    assert "Не понял" in session.last_text()
    session.clear()
    await say(app, "@norm_test_bot беру пост на пятницу", stranger)
    assert not friday_post(app).assigned


async def test_photo_caption_is_read_like_text(ready, session):
    app = ready
    message = Message(
        message_id=7001,
        chat=Chat(id=WORK_CHAT, type="supergroup"),
        from_user=make_user(**{"user_id": 101, "username": "cw_alpha", "name": "Альфа"}),
        caption="беру пост на пятницу",
    )
    await feed(app, Update(update_id=7001, message=message))
    assert friday_post(app).assignee_username == "cw_alpha"
    empty = Message(
        message_id=7002,
        chat=Chat(id=WORK_CHAT, type="supergroup"),
        from_user=make_user(101, "cw_alpha", "Альфа"),
    )
    session.clear()
    await feed(app, Update(update_id=7002, message=empty))  # стикер или служебное сообщение
    assert session.calls == []


async def test_buttons_in_foreign_chats_are_ignored(ready, session):
    app = ready
    post = friday_post(app)
    await feed(
        app,
        callback_update(f"claim:{post.id}", message_id=5, chat_id=-1009999999999, **ALPHA),
    )
    assert session.calls == [] and not app.tracker.get(post.id).assigned


async def test_replies_stay_in_the_topic_they_were_asked_in(ready, session):
    app = ready
    asked = text_update("/today", **ALPHA)
    asked.message.message_thread_id = 12
    asked.message.is_topic_message = True
    await feed(app, asked)
    assert session.named("SendMessage")[-1]["message_thread_id"] == 12

    reply_chain = text_update("/today", **ALPHA)
    reply_chain.message.message_thread_id = 12  # цепочка ответов в обычной группе — не тема
    session.clear()
    await feed(app, reply_chain)
    assert "message_thread_id" not in session.named("SendMessage")[-1]

    post = friday_post(app)
    pressed = callback_update(f"claim:{post.id}", message_id=session._next_id, **ALPHA)
    pressed.callback_query.message.message_thread_id = 12
    pressed.callback_query.message.is_topic_message = True
    session.clear()
    await feed(app, pressed)
    assert app.tracker.get(post.id).assigned
    assert all(body.get("message_thread_id") == 12 for body in session.named("SendMessage"))


async def test_bot_status_change_rechecks_its_rights(ready, session):
    app = ready
    session.can_pin = False  # бота сделали администратором, но без права закреплять
    event = ChatMemberUpdated(
        chat=Chat(id=WORK_CHAT, type="supergroup"),
        old_chat_member=ChatMember(status="member"),
        new_chat_member=ChatMember(status="administrator", can_pin_messages=False),
    )
    await feed(app, Update(update_id=7004, my_chat_member=event))
    assert session.named("GetChatMember")
    assert any("нет права закреплять" in warning for warning in app.info.warnings)
    session.can_pin = True
    await feed(app, Update(update_id=7005, my_chat_member=event))
    assert not any("нет права закреплять" in warning for warning in app.info.warnings)


# --- доска -------------------------------------------------------------------------------------


async def test_board_is_published_pinned_and_refreshed(ready, session):
    app = ready
    await say(app, "/board", ALPHA)
    assert session.named("SendMessage")[-1]["text"].startswith("📌 Доска")
    assert session.named("PinChatMessage")[0]["disable_notification"] is True
    board_id = int(app.tracker.state.get_meta("board_message_id"))

    session.clear()
    await say(app, "беру пост на пятницу", ALPHA)
    await app.refresh_board()
    edit = session.named("EditMessageText")[-1]
    assert edit["message_id"] == board_id and "Копирайтер Альфа" in edit["text"]

    session.clear()
    await say(app, "/board", ALPHA)  # повторная команда обновляет ту же доску
    assert session.named("PinChatMessage") == []
    assert "Доска обновлена" in session.last_text()


async def test_pin_failure_is_explained(ready, session):
    ready_app = ready
    session.reject_pin = True
    await say(ready_app, "/board", ALPHA)
    assert "Не смог закрепить доску" in session.last_text()


# --- расписание ---------------------------------------------------------------------------------


async def test_scheduler_sends_summary_once(ready, session, clock):
    app = ready
    clock.set(2026, 9, 29, 9, 30)
    await app.tick()
    texts = session.sent_texts()
    assert len(texts) == 1 and texts[0].startswith("☀️ Вторник, 29 сентября")
    session.clear()
    await app.tick()
    assert session.calls == []
    assert app.tracker.state.get_meta("heartbeat")


async def test_scheduler_retries_when_send_fails(ready, session, clock, monkeypatch):
    app = ready
    clock.set(2026, 9, 29, 9, 30)

    async def broken_send(*args, **kwargs):
        return None

    real_send = app.sender.send
    monkeypatch.setattr(app.sender, "send", broken_send)
    await app.tick()
    monkeypatch.setattr(app.sender, "send", real_send)
    clock.set(2026, 9, 29, 9, 35)
    await app.tick()  # на следующем такте, пока не вышло время, догоняет
    assert any(t.startswith("☀️") for t in session.sent_texts())


async def test_soft_start_keeps_escalations_silent(ready, session, clock):
    app = ready
    app.tracker.state.set_meta("first_run", "2026-09-29")
    clock.set(2026, 9, 29, 16, 0)  # D−4 для поста на 05.10: напоминание «никто не взял»
    await app.tick()
    assert session.sent_texts() == []
    clock.set(2026, 9, 29, 9, 30)
    app.tracker.state.set_meta("first_run", "2026-09-29")
    await app.tick()
    assert session.sent_texts()[0].startswith("☀️")  # саммари в мягком старте идёт


async def test_first_tick_marks_the_start_of_soft_mode(app, session, clock):
    app.tracker.state.set_meta("first_run", None)
    await app.startup()
    await app.tick()
    assert app.tracker.state.get_meta("first_run") == "2026-09-29"


async def test_downtime_is_announced(app, session, clock):
    app.tracker.state.set_meta("heartbeat", (clock.current - timedelta(hours=30)).isoformat())
    await app.startup()
    assert "Меня не было около 30 ч" in session.last_text()


async def test_startup_reports_missing_rights(app, session):
    session.reads_all = False
    session.can_pin = False
    await app.startup()
    assert any("закреплять" in w for w in app.info.warnings)


# --- доки, копия трекера, календарь ------------------------------------------------------------


def doc_comment(cid, text, author="Клиент Один"):
    return ExternalComment(
        id=f"doc:docC:{cid}", source="doc", author=author, text=text,
        created=datetime(2026, 9, 29, 9, 0), resolved=False, doc_id="docC",
    )  # fmt: skip


async def test_doc_comments_first_poll_is_silent_then_new_ones_are_announced(ready, session):
    app = ready
    app.google.docs["docC"] = [doc_comment("old", "старый комментарий")]
    await app.poll_docs_once()
    assert session.sent_texts() == []
    app.google.docs["docC"].append(doc_comment("new", "поправьте вторую строку"))
    await app.poll_docs_once()
    text = session.last_text()
    assert "Новые комментарии клиента" in text and "поправьте вторую строку" in text
    assert "старый комментарий" not in text
    session.clear()
    await app.poll_docs_once()
    assert session.sent_texts() == []  # второй раз о том же не пишем


async def test_denied_doc_is_reported_once(ready, session):
    app = ready

    def denied(doc_id):
        raise GoogleError(404, "не найден")

    app.google.doc_comments = denied
    await app.poll_docs_once()
    await app.poll_docs_once()
    assert sum("Нет доступа к доку" in w for w in app.info.warnings) == 1


async def test_tracker_copy_is_written_to_google(ready):
    app = ready
    await app.push_sheet_once()
    written = app.google.written[-1]
    assert written[0][0] == "№" and len(written) > 5
    assert app.info.last_tracker_copy is not None and not app.info.last_tracker_copy_error


async def test_tracker_copy_errors_do_not_break_the_bot(ready):
    app = ready

    def broken(*args, **kwargs):
        raise GoogleError(403, "нет прав")

    app.google.replace_values = broken
    await app.push_sheet_once()
    assert "нет прав" in app.info.last_tracker_copy_error


async def test_bot_id_is_known_after_startup(app, session):
    await app.startup()
    assert app.bot_id == BOT_ID


async def test_summary_waits_for_the_first_kp_read(app, session, clock):
    app.tracker.state.set_meta("first_run", "2026-09-01")
    await app.startup()
    session.clear()
    clock.set(2026, 9, 29, 10, 0)
    app.info.started_at = clock.current  # бот только что запущен
    await app.tick()
    assert session.sent_texts() == []  # КП ещё не прочитано: саммари было бы пустым
    await app.apply_kp_data(app.google.xlsx)
    session.clear()
    await app.tick()
    assert session.sent_texts()[0].startswith("☀️")


async def test_summary_goes_out_after_five_minutes_even_without_kp(app, session, clock):
    app.tracker.state.set_meta("first_run", "2026-09-01")
    await app.startup()
    session.clear()
    clock.set(2026, 9, 29, 10, 0)
    app.info.started_at = clock.current - timedelta(minutes=6)  # КП всё ещё не читается
    await app.tick()
    assert session.sent_texts()[0].startswith("☀️")


async def test_app_starts_all_loops_and_stops_cleanly(app, session, clock, monkeypatch):
    """Весь запуск целиком: проверка бота, циклы КП, доков, напоминаний, доски и таблицы."""
    monkeypatch.setattr("norm_tasker.bot.app.refresh_calendar", lambda *args, **kwargs: None)
    app.tracker.state.set_meta("first_run", "2026-09-01")
    app.delay_scale = 0  # не ждать паузы перед первым запуском циклов
    running = asyncio.create_task(app.run(handle_signals=False))
    await asyncio.sleep(0.6)
    assert app.bot_id == BOT_ID and len(app._tasks) == 6
    assert all(not task.done() for task in app._tasks)
    assert app.tracker.state.get_meta("heartbeat")  # цикл напоминаний отработал
    assert app.tracker.count_posts("kp") >= 8 and app.info.last_kp_sync  # цикл КП отработал
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert all(task.cancelled() or task.done() for task in app._tasks)


# --- ИИ: вопросы боту по имени -------------------------------------------------------------


@pytest.fixture
def gemini():
    return FakeTransport()


@pytest.fixture
async def ai_ready(ready, gemini):
    """Бот с подключённым (подставным) Gemini; журнал вызовов пуст."""
    ready.ai = Assistant(GeminiClient(KEY, transport=gemini), ready.tracker)
    ready.info.ai_configured = True
    return ready


async def ask_bot(app, text, who=ALPHA, **kwargs):
    """Сообщение в чат и ожидание фонового ответа ИИ."""
    await say(app, text, who, **kwargs)
    await app.wait_background()


async def test_mention_is_answered_by_gemini_from_the_tracker_data(ai_ready, session, gemini):
    app = ai_ready
    gemini.replies.append((200, answer("Ближайший — **пятничный** пост: №1 <тест> & всё")))
    await ask_bot(app, "@norm_test_bot кто что делает на этой неделе?")
    assert session.named("SendChatAction")[0]["action"] == "typing"
    reply = session.named("SendMessage")[-1]
    assert "<b>пятничный</b>" in reply["text"] and "&lt;тест&gt; &amp; всё" in reply["text"]
    assert reply["reply_parameters"]["message_id"]
    prompt = gemini.prompt()
    assert "ВОПРОС\nкто что делает на этой неделе?" in prompt and "@norm_test_bot" not in prompt
    assert "Сегодня: вт 29.09.2026, сейчас 10:00" in prompt
    assert "Спрашивает: Копирайтер Альфа (копирайтер)" in prompt
    assert "Ответственный — ответственный за проект" in prompt
    assert f"№{friday_post(app).id} | пт 02.10" in prompt
    assert app.info.ai_error is None and app.info.ai_model


async def test_gemini_failure_falls_back_to_keywords_and_shows_in_status(ai_ready, session, gemini):
    app = ai_ready
    bad_key = error_body("INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.")
    gemini.replies.append((400, bad_key))
    await ask_bot(app, "@norm_test_bot чё там по задачам")
    assert "📌 Доска" in session.last_text()  # ответ как на /week
    assert "не принял ключ" in app.info.ai_error
    session.clear()
    await say(app, "/status", ALPHA)
    assert "⚠️ ИИ не отвечает: Gemini не принял ключ" in session.last_text()


async def test_gemini_is_not_asked_without_a_mention_or_for_commands_and_reports(
    ai_ready, session, gemini
):
    app = ai_ready
    await ask_bot(app, "чё там по задачам")
    await ask_bot(app, "/week")
    await ask_bot(app, "@norm_test_bot беру пост на пятницу")
    await ask_bot(app, "@norm_test_bot", BETA)  # одно обращение без вопроса: подсказка
    assert gemini.calls == []
    assert friday_post(app).assignee_username == "cw_alpha"
    assert "Не понял" in session.last_text()


async def test_gemini_questions_per_hour_are_limited(ai_ready, session, gemini, monkeypatch):
    app = ai_ready
    monkeypatch.setattr("norm_tasker.bot.app.AI_CALLS_PER_HOUR", 1)
    gemini.replies.append((200, answer("Первый ответ")))
    await ask_bot(app, "@norm_test_bot вопрос раз")
    assert "Первый ответ" in session.last_text()
    await ask_bot(app, "@norm_test_bot чё по задачам")  # лимит выбран: отвечают команды
    assert "📌 Доска" in session.last_text() and len(gemini.calls) == 1


async def test_reply_to_the_bot_is_answered_by_gemini_about_that_post(ai_ready, session, gemini):
    app = ai_ready
    post = friday_post(app)
    replied = linked_bot_message(app, post)
    replied.text = "Кто возьмёт пятничный пост?"
    gemini.replies.append((200, answer("Срок — до среды")))
    await ask_bot(app, "а когда срок?", reply_to=replied)
    assert "Срок — до среды" in session.last_text()
    assert session.named("SendMessage")[-1]["reply_parameters"]["message_id"]
    prompt = gemini.prompt()
    assert "ВОПРОС\nа когда срок?" in prompt
    focus = prompt.split("Вопрос задан в ответ на сообщение бота")[1]
    assert f"№{post.id} | пт 02.10" in focus
    # Владелец разрешил отправлять в Gemini всё: само сообщение тоже уходит, с указанием автора.
    assert "СООБЩЕНИЕ, НА КОТОРОЕ ОТВЕТИЛИ (бот)\nКто возьмёт пятничный пост?" in prompt
    # Сообщение, о постах которого бот не помнит: блока с постами нет.
    gemini.replies.append((200, answer("ок")))
    await ask_bot(app, "что нового?", reply_to=bot_message(778))
    assert "Вопрос задан в ответ" not in gemini.prompt()


async def test_thanks_in_reply_are_not_sent_to_gemini(ai_ready, session, gemini):
    await ask_bot(ai_ready, "спасибо!", reply_to=bot_message(777))
    assert gemini.calls == [] and session.named("SendMessage") == []


async def test_reply_to_a_long_list_is_not_about_one_post(ai_ready, gemini, monkeypatch):
    app = ai_ready
    monkeypatch.setattr("norm_tasker.ai.assistant.MAX_FOCUS", 1)
    first, second = app.tracker.open_posts(app.tracker.today())[:2]
    now = app.tracker.now()
    app.tracker.state.remember_message(WORK_CHAT, 780, "free", [first.id, second.id], now)
    app.tracker.state.remember_message(WORK_CHAT, 781, "confirm", [first.id], now)
    gemini.replies.extend([(200, answer("ок")), (200, answer("ок"))])
    await ask_bot(app, "что тут?", reply_to=bot_message(780))
    assert "Вопрос задан в ответ" not in gemini.prompt()
    await ask_bot(app, "что тут?", reply_to=bot_message(781))
    assert "Вопрос задан в ответ" in gemini.prompt()


async def test_client_comments_and_documents_are_not_sent_to_gemini(ai_ready, gemini):
    app = ai_ready
    app.google.docs["docC"] = [doc_comment("c1", "поправьте вторую строку")]
    await app.poll_docs_once()
    gemini.replies.append((200, answer("ок")))
    await ask_bot(app, "@norm_test_bot что нового?")
    assert "поправьте" not in gemini.prompt() and "docs.google.com" not in gemini.prompt()


async def test_someone_outside_the_team_gets_answers_but_not_my_posts(ai_ready, session, gemini):
    app = ai_ready
    app.settings.unknown_role = None
    stranger = {"user_id": 999, "username": "stranger_x", "name": "Гость"}
    gemini.replies.append((200, answer("Есть три поста")))
    await ask_bot(app, "@norm_test_bot что у нас на неделе?", stranger)
    assert "Есть три поста" in session.last_text()
    assert "Спрашивает" not in gemini.prompt()


async def test_startup_check_reports_the_model_or_the_error(ai_ready, session, gemini):
    app = ai_ready
    gemini.replies.append((200, answer("ок")))
    await app.check_ai()
    await say(app, "/status", ALPHA)
    assert "🧠 ИИ: Gemini, модель gemini-flash-lite-latest" in session.last_text()
    assert app.info.ai_ok_at is not None
    denied = error_body("PERMISSION_DENIED", "User location is not supported", 403)
    gemini.replies.append((403, denied))
    await app.check_ai()
    session.clear()
    await say(app, "/status", ALPHA)
    assert "⚠️ ИИ не отвечает: Gemini недоступен из региона" in session.last_text()


# --- ИИ понимает обычные фразы --------------------------------------------------------------

BOSS = {"user_id": 104, "username": "boss_user", "name": "Т"}
STRANGER = {"user_id": 999, "username": "stranger_x", "name": "Гость"}
REPORT = "по пятничному посту я закончила, теперь у Алисы"  # правила такую фразу не узнают


def stage_verdict(post, stage, confidence="high"):
    action = {"type": "stage", "posts": [post.id], "stage": stage}
    return verdict(kind="action", confidence=confidence, actions=[action])


class GatedTransport(FakeTransport):
    """Не отвечает, пока тест не разрешит: видно, что бот делает, пока Gemini «думает»."""

    def __init__(self, *replies):
        super().__init__(*replies)
        self.gate = threading.Event()

    def post(self, url, headers, payload, timeout):
        assert self.gate.wait(10)
        return super().post(url, headers, payload, timeout)


async def test_done_in_reply_to_the_bot_is_understood_by_gemini(ai_ready, session, gemini):
    app = ai_ready
    post = friday_post(app)
    await say(app, "беру пост на пятницу", BETA)
    bot_reply = bot_message(session._next_id)
    session.clear()
    gemini.replies.append(stage_verdict(post, "text_shown"))
    await ask_bot(app, "готово", BETA, reply_to=bot_reply)
    assert app.tracker.get(post.id).stage == Stage.TEXT_SHOWN
    sent = session.named("SendMessage")[-1]
    assert "текст у клиента" in sent["text"]  # записанное видно всем словами, а не одним 👍
    assert sent["reply_markup"]["inline_keyboard"][0][-1]["callback_data"].startswith("undo:")
    prompt = gemini.prompt()
    assert "РЕЖИМ\nК боту обратились напрямую" in prompt and "ВОПРОС\nготово" in prompt
    assert f"№{post.id} | пт 02.10" in prompt.split("Вопрос задан в ответ")[1]
    assert session.named("SendChatAction")[0]["action"] == "typing"


async def test_request_by_the_bots_name_is_carried_out_and_can_be_undone(ai_ready, session, gemini):
    app = ai_ready
    post = friday_post(app)
    assert app.bot_names == ("бот", "Тест", "norm_test")
    gemini.replies.append(stage_verdict(post, "at_designer"))
    await ask_bot(app, "бот, закинь пятничный пост дизайнеру", ALPHA)
    assert app.tracker.get(post.id).stage == Stage.AT_DESIGNER
    assert "у дизайнера" in session.last_text()
    session.clear()
    await feed(app, callback_update(last_undo(app, post), message_id=session._next_id, **ALPHA))
    assert app.tracker.get(post.id).stage == Stage.NEW


def last_undo(app, post):
    return f"undo:{app.tracker.history(post.id)[0].id}"


async def test_gemini_can_answer_or_ask_a_clarifying_question(ai_ready, session, gemini):
    app = ai_ready
    gemini.replies.append(verdict(kind="clarify", text="Про какой пост: №1 или №2?"))
    await ask_bot(app, "бот, отметь что готово", ALPHA)
    assert session.last_text() == "Про какой пост: №1 или №2?"
    assert friday_post(app).stage == Stage.NEW
    gemini.replies.append(verdict(kind="answer", text="У вас **один** пост & всё"))
    await ask_bot(app, "бот, что у меня?", ALPHA)
    assert session.last_text() == "У вас <b>один</b> пост &amp; всё"
    gemini.replies.append(verdict(kind="ignore"))  # позвали, а модель не поняла: подсказка команд
    await ask_bot(app, "бот, ну", ALPHA)
    assert "Не понял" in session.last_text()


async def test_report_in_the_general_chat_is_recorded_only_when_gemini_is_sure(
    ai_ready, session, gemini
):
    app = ai_ready
    post = friday_post(app)
    await say(app, "беру пост на пятницу", ALPHA)
    session.clear()
    for quiet in (
        stage_verdict(post, "text_shown", confidence="low"),  # не уверен
        verdict(kind="answer", text="Поздравляю!"),  # в общем чате бот не болтает
        verdict(kind="clarify", text="Какой пост?"),  # и не переспрашивает
        verdict(kind="ignore"),
    ):
        gemini.replies.append(quiet)
        await ask_bot(app, REPORT, ALPHA)
    assert session.named("SendMessage") == [] and session.named("SendChatAction") == []
    assert app.tracker.get(post.id).stage == Stage.TAKEN
    assert len(gemini.calls) == 4

    gemini.replies.append(stage_verdict(post, "text_shown"))
    await ask_bot(app, REPORT, ALPHA)
    assert app.tracker.get(post.id).stage == Stage.TEXT_SHOWN
    assert "текст у клиента" in session.last_text()
    assert session.named("SendMessage")[-1]["reply_parameters"]["message_id"]
    prompt = gemini.prompt()
    assert "РЕЖИМ\nК боту не обращались" in prompt
    assert f"СООБЩЕНИЕ ИЗ ЧАТА\n{REPORT}" in prompt and "Пишет: Копирайтер Альфа" in prompt
    assert app.tracker.history(post.id)[0].source == "chat"


async def test_chatter_and_the_off_switch_keep_messages_away_from_gemini(ai_ready, session, gemini):
    app = ai_ready
    for text in ("всем привет, как выходные?", "кто на обед?", "спасибо", "ок", "😂😂"):
        await ask_bot(app, text, ALPHA)
    await ask_bot(app, "беру пост на пятницу", ALPHA)  # это и так понимают правила
    await ask_bot(app, "текст на пятницу готов, отправил клиенту", ALPHA)
    assert gemini.calls == []
    app.env = dataclasses.replace(app.env, ai_listen=False)  # AI_LISTEN=0
    await ask_bot(app, REPORT, BETA)
    assert gemini.calls == []
    gemini.replies.append(verdict(kind="answer", text="Слушаю"))  # но на обращение бот отвечает
    await ask_bot(app, "бот, привет", ALPHA)
    assert session.last_text() == "Слушаю"


async def test_boss_and_strangers_are_not_read_and_cannot_record(ai_ready, session, gemini):
    app = ai_ready
    app.settings.unknown_role = None
    post = friday_post(app)
    for who in (BOSS, STRANGER):
        await ask_bot(app, REPORT, who)
    assert gemini.calls == []
    for who in (BOSS, STRANGER):
        gemini.replies.append(stage_verdict(post, "text_shown"))
        await ask_bot(app, "бот, по пятничному всё готово", who)
        assert session.last_text() == views.NOT_ALLOWED
    assert friday_post(app).stage == Stage.NEW


async def test_recent_chat_and_the_quoted_message_go_along(ai_ready, gemini):
    app = ai_ready
    gemini.replies.extend([verdict(kind="ignore"), verdict(kind="ignore")])
    await ask_bot(app, "кто-нибудь брал пост про фильм и профессии?", BETA)
    assert "ПОСЛЕДНИЕ СООБЩЕНИЯ ЧАТА" not in gemini.prompt()
    await ask_bot(app, "скоро покажу клиенту текст про фильм", ALPHA)
    assert (
        "ПОСЛЕДНИЕ СООБЩЕНИЯ ЧАТА (старые первыми)\n"
        "[10:00] Копирайтер Бета: кто-нибудь брал пост про фильм и профессии?"
    ) in gemini.prompt()
    colleague = Message(
        message_id=888,
        chat=Chat(id=WORK_CHAT, type="supergroup"),
        from_user=make_user(102, "cw_beta", "Бета"),
        text="кто взял пост?",
    )
    gemini.replies.append(verdict(kind="answer", text="Никто"))
    await ask_bot(app, "@norm_test_bot это про какой пост?", ALPHA, reply_to=colleague)
    assert "СООБЩЕНИЕ, НА КОТОРОЕ ОТВЕТИЛИ (Бета)\nкто взял пост?" in gemini.prompt()


async def test_new_post_by_request_and_a_request_that_cannot_be_done(ai_ready, session, gemini):
    app = ai_ready
    add = {"type": "add_post", "date": "2026-10-15", "topic": "Акция недели"}
    gemini.replies.append(verdict(kind="action", confidence="high", actions=[add]))
    await ask_bot(app, "бот, добавь пост на 15.10 про акцию недели", ALPHA)
    [added] = app.tracker.posts_on(date(2026, 10, 15))
    assert added.topic == "Акция недели" and session.last_text().startswith("➕ Добавлен пост")

    ghost = {"type": "stage", "posts": [9999], "stage": "text_ok"}
    gemini.replies.append(verdict(kind="action", confidence="high", actions=[ghost]))
    await ask_bot(app, "бот, по посту 9999 клиент одобрил текст", ALPHA)
    assert session.last_text() == views.NOT_RECORDED


async def test_same_request_in_the_general_chat_cannot_add_posts(ai_ready, session, gemini):
    app = ai_ready
    add = {"type": "add_post", "date": "2026-10-15", "topic": "Случайный"}
    gemini.replies.append(verdict(kind="action", confidence="high", actions=[add]))
    await ask_bot(app, "надо бы добавить пост на 15.10 про акцию", ALPHA)
    assert app.tracker.posts_on(date(2026, 10, 15)) == [] and session.named("SendMessage") == []


async def test_gemini_failure_while_listening_is_silent_but_visible_in_status(
    ai_ready, session, gemini
):
    app = ai_ready
    gemini.replies.append((429, error_body("RESOURCE_EXHAUSTED", "quota", 429)))
    await ask_bot(app, REPORT, ALPHA)
    assert session.named("SendMessage") == []
    await say(app, "/status", ALPHA)
    assert "исчерпан лимит запросов" in session.last_text()


async def test_messages_wait_for_gemini_one_by_one_and_in_order(ready, session):
    transport = GatedTransport((200, answer("Первый")), (200, answer("Второй")))
    ready.ai = Assistant(GeminiClient(KEY, transport=transport), ready.tracker)
    await say(ready, "@norm_test_bot первый вопрос", ALPHA)
    await say(ready, "@norm_test_bot второй вопрос", BETA)
    assert ready.ai_waiting == 2 and session.sent_texts() == []
    transport.gate.set()
    await ready.wait_background()
    assert session.sent_texts() == ["Первый", "Второй"] and ready.ai_waiting == 0


async def test_busy_gemini_does_not_pile_up_chatter_but_answers_questions(
    ready, session, monkeypatch
):
    monkeypatch.setattr("norm_tasker.bot.handlers.MAX_LISTEN_BACKLOG", 1)
    transport = GatedTransport(verdict(kind="ignore"), (200, answer("Отвечаю")))
    ready.ai = Assistant(GeminiClient(KEY, transport=transport), ready.tracker)
    await say(ready, REPORT, ALPHA)  # уходит в Gemini и ждёт ответа
    await say(ready, REPORT, BETA)  # Gemini занят: разговор пропускаем
    await say(ready, "@norm_test_bot вопрос важнее", ALPHA)  # вопрос к боту принимается всегда
    transport.gate.set()
    await ready.wait_background()
    assert len(transport.calls) == 2 and session.last_text() == "Отвечаю"


async def test_listening_has_its_own_hourly_limit(ai_ready, session, gemini, monkeypatch):
    app = ai_ready
    monkeypatch.setattr("norm_tasker.bot.app.AI_LISTEN_PER_HOUR", 1)
    gemini.replies.append(verdict(kind="ignore"))
    for _ in range(3):
        await ask_bot(app, REPORT, ALPHA)
    assert len(gemini.calls) == 1
    gemini.replies.append((200, answer("Отвечаю")))  # вопросы к боту считаются отдельно
    await ask_bot(app, "@norm_test_bot привет", ALPHA)
    assert session.last_text() == "Отвечаю"


async def test_team_is_told_once_that_ai_reads_the_chat(app, session, gemini):
    app.ai = Assistant(GeminiClient(KEY, transport=gemini), app.tracker)
    await app.startup()
    notices = [t for t in session.sent_texts() if "Gemini, Google" in t]
    assert len(notices) == 1 and "Пишите мне как коллеге" in notices[0]
    await app.startup()  # перезапуск: второй раз не предупреждаем
    assert len([t for t in session.sent_texts() if "Gemini, Google" in t]) == 1


async def test_no_warning_when_the_ai_does_not_read_the_chat(app, session, gemini):
    app.ai = Assistant(GeminiClient(KEY, transport=gemini), app.tracker)
    app.env = dataclasses.replace(app.env, ai_listen=False)
    await app.startup()
    assert not [t for t in session.sent_texts() if "Gemini" in t]


async def test_help_and_status_tell_how_to_talk_to_the_bot(ready, session, gemini):
    app = ready
    await say(app, "/help", ALPHA)
    assert "позовите меня по имени или ответьте" in session.last_text()
    app.ai = Assistant(GeminiClient(KEY, transport=gemini), app.tracker)
    app.info.ai_configured = True
    await say(app, "/help", ALPHA)
    assert "говорите со мной как с коллегой" in session.last_text()
    await say(app, "/status", ALPHA)
    assert "Читаю сообщения чата про посты и записываю отчёты сам" in session.last_text()
    app.env = dataclasses.replace(app.env, ai_listen=False)
    await say(app, "/status", ALPHA)
    assert "Сообщения, обращённые не ко мне, не читаю" in session.last_text()


async def test_status_says_when_ai_is_off(ready, session):
    await say(ready, "/status", ALPHA)
    assert "ИИ не подключён" in session.last_text()


def test_build_app_connects_gemini_only_with_a_key_that_is_on(tmp_path, settings):
    from norm_tasker.bot.app import build_app

    base = {
        "bot_token": "42:TEST",
        "data_dir": tmp_path,
        "config_path": tmp_path / "config.yaml",
        "google_credentials": None,
        "kp_spreadsheet_id": None,
        "tracker_spreadsheet_id": None,
    }
    assert build_app(Env(**base), settings).ai is None
    assert build_app(Env(**base, gemini_key=KEY, ai_enabled=False), settings).ai is None
    app = build_app(
        Env(**base, gemini_key=KEY, gemini_model="my-model", gemini_url="https://proxy.example"),
        settings,
    )
    assert app.ai is not None and app.info.ai_configured
    assert app.ai.client.model == "my-model" and app.ai.client.base_url == "https://proxy.example"
