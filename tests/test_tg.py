"""Клиент Telegram: разбор апдейтов и команд, ошибки API, цикл опроса.

Сеть проверяется по-настоящему: на localhost поднимается подставной сервер с тем же форматом
ответов, что у Bot API, — так видно, что уходит по HTTP и как разбирается ответ.
"""

import asyncio
import threading
import time
from typing import Any

import pytest

from norm_tasker.tg.api import Api, HttpTransport, in_thread
from norm_tasker.tg.commands import parse_command
from norm_tasker.tg.errors import (
    BadRequest,
    Conflict,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    Unauthorized,
    error_for,
)
from norm_tasker.tg.polling import poll_updates
from norm_tasker.tg.types import Update
from tg_fixture import FakeTelegram

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


def make_api(server: FakeTelegram) -> Api:
    return Api(TOKEN, transport=HttpTransport(retries=1, backoff=0), base_url=server.url)


# --- типы и команды ---------------------------------------------------------------------------
def test_update_is_parsed_from_telegram_json():
    update = Update.from_dict(
        {
            "update_id": 7,
            "message": {
                "message_id": 30,
                "date": 1780000000,
                "chat": {"id": -1001234567890, "type": "supergroup", "title": "Команда"},
                "from": {"id": 5, "is_bot": False, "first_name": "Иван", "last_name": "Петров"},
                "text": "беру пост на пятницу",
                "message_thread_id": 12,
                "is_topic_message": True,
                "reply_to_message": {
                    "message_id": 29,
                    "chat": {"id": -1001234567890, "type": "supergroup"},
                    "from": {"id": 42, "is_bot": True, "first_name": "Бот"},
                    "text": "сообщение бота",
                },
            },
        }
    )
    message = update.message
    assert message is not None and message.text == "беру пост на пятницу"
    assert message.from_user is not None and message.from_user.full_name == "Иван Петров"
    assert message.chat.id == -1001234567890 and message.chat.type == "supergroup"
    assert message.is_topic_message and message.message_thread_id == 12
    assert message.reply_to_message is not None and message.reply_to_message.from_user.is_bot
    assert update.callback_query is None and update.my_chat_member is None


def test_callback_and_membership_updates_are_parsed():
    callback = Update.from_dict(
        {
            "update_id": 8,
            "callback_query": {
                "id": "991",
                "from": {"id": 5, "is_bot": False, "first_name": "Иван", "username": "ivan"},
                "message": {"message_id": 31, "date": 0, "chat": {"id": -100, "type": "group"}},
                "data": "undo:5",
            },
        }
    ).callback_query
    assert callback is not None and callback.id == "991" and callback.data == "undo:5"
    assert callback.message is not None  # у старого сообщения есть чат и номер — этого хватает
    assert callback.message.chat.id == -100 and callback.message.message_id == 31

    member = Update.from_dict(
        {
            "update_id": 9,
            "my_chat_member": {
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 5, "is_bot": False, "first_name": "Иван"},
                "old_chat_member": {"status": "member", "user": {"id": 42, "is_bot": True}},
                "new_chat_member": {
                    "status": "administrator",
                    "user": {"id": 42, "is_bot": True},
                    "can_pin_messages": True,
                },
            },
        }
    ).my_chat_member
    assert member is not None and member.new_chat_member.status == "administrator"
    assert member.new_chat_member.can_pin_messages is True


def test_unknown_update_kinds_parse_to_nothing():
    update = Update.from_dict({"update_id": 10, "edited_message": {"message_id": 1}})
    assert update.message is None and update.callback_query is None


def test_command_parsing():
    command = parse_command("/post@Norm_Bot 16.10 тема", "norm_bot")
    assert command is not None and command.name == "post" and command.args == "16.10 тема"
    assert parse_command("/Today").name == "today"  # регистр не важен
    assert parse_command("/undo  ").args == ""
    multi = parse_command("/add 15.10 первая строка\nвторая строка")
    assert multi is not None and multi.args == "15.10 первая строка\nвторая строка"
    assert parse_command("/help@other_bot", "norm_bot") is None  # адресовано другому боту
    assert parse_command("/help@other_bot").name == "help"  # имя своего бота пока неизвестно
    assert parse_command("беру /post") is None
    assert parse_command("/") is None and parse_command("") is None and parse_command(None) is None
    assert parse_command("//x") is None


# --- ошибки ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, BadRequest),
        (401, Unauthorized),
        (403, Forbidden),
        (404, Unauthorized),  # токен неверного вида Telegram отвечает «Not Found»
        (409, Conflict),
        (429, RetryAfter),
        (502, NetworkError),
        (418, TelegramError),
    ],
)
def test_error_statuses_map_to_typed_errors(status, kind):
    error = error_for(
        "sendMessage", status, {"ok": False, "error_code": status, "description": "x"}
    )
    assert type(error) is kind and error.code == status


def test_retry_after_and_missing_body():
    error = error_for("m", 429, {"ok": False, "parameters": {"retry_after": 7}})
    assert isinstance(error, RetryAfter) and error.retry_after == 7
    assert isinstance(error_for("m", 200, None), NetworkError)  # ответ не похож на JSON
    assert isinstance(error_for("m", 503, "<html>"), NetworkError)


async def test_get_me_over_http(telegram):
    telegram.reply(
        "getMe",
        {"id": 42, "is_bot": True, "first_name": "Т", "username": "t_bot",
         "can_read_all_group_messages": True},
    )  # fmt: skip
    me = await make_api(telegram).get_me()
    assert me.id == 42 and me.username == "t_bot" and me.can_read_all_group_messages
    request = telegram.requests[0]
    assert request["path"] == f"/bot{TOKEN}/getMe"
    assert request["content_type"] == "application/json"


async def test_send_message_body_matches_the_bot_api(telegram):
    telegram.reply(
        "sendMessage", {"message_id": 501, "date": 1, "chat": {"id": -100, "type": "supergroup"}}
    )
    api = make_api(telegram)
    sent = await api.send_message(
        -100,
        "✍️ <b>Привет</b>",
        reply_markup={"inline_keyboard": [[{"text": "Отменить", "callback_data": "undo:1"}]]},
        reply_to=77,
        thread_id=12,
    )
    assert sent.message_id == 501
    body = telegram.requests[0]["body"]
    assert body["chat_id"] == -100 and body["text"] == "✍️ <b>Привет</b>"
    assert body["parse_mode"] == "HTML"
    assert body["reply_parameters"] == {"message_id": 77, "allow_sending_without_reply": True}
    assert body["message_thread_id"] == 12
    assert body["link_preview_options"] == {"is_disabled": True}
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "undo:1"

    telegram.reply("sendMessage", {"message_id": 502, "date": 1, "chat": {"id": -100, "type": "x"}})
    await api.send_message(-100, "просто текст", parse_mode=None)
    plain = telegram.requests[1]["body"]
    assert "parse_mode" not in plain and "reply_markup" not in plain  # пустое не отправляем
    assert "reply_parameters" not in plain and "message_thread_id" not in plain


async def test_other_methods_send_what_telegram_expects(telegram):
    api = make_api(telegram)
    await api.set_message_reaction(-100, 5, "👍")
    await api.pin_chat_message(-100, 5)
    await api.answer_callback_query("991", "Готово", show_alert=True)
    await api.edit_message_text(-100, 5, "новый текст")
    await api.remove_buttons(-100, 5)
    bodies = {r["method"]: r["body"] for r in telegram.requests}
    assert bodies["setMessageReaction"]["reaction"] == [{"type": "emoji", "emoji": "👍"}]
    assert bodies["pinChatMessage"]["disable_notification"] is True
    assert bodies["answerCallbackQuery"] == {
        "callback_query_id": "991", "text": "Готово", "show_alert": True,
    }  # fmt: skip
    assert "reply_markup" not in bodies["editMessageText"]  # кнопки снимаются
    assert bodies["editMessageReplyMarkup"] == {"chat_id": -100, "message_id": 5}


async def test_get_updates_asks_for_long_polling(telegram):
    telegram.reply("getUpdates", [{"update_id": 1}])
    updates = await make_api(telegram).get_updates(5, 25, ["message", "callback_query"])
    assert updates == [{"update_id": 1}]
    assert telegram.requests[0]["body"] == {
        "offset": 5, "timeout": 25, "allowed_updates": ["message", "callback_query"],
    }  # fmt: skip


async def test_error_replies_over_http_become_typed_errors(telegram):
    api = make_api(telegram)
    telegram.fail("sendMessage", 400, "Bad Request: can't parse entities: unexpected end tag")
    with pytest.raises(BadRequest, match="can't parse entities"):
        await api.send_message(-100, "<b>")
    telegram.fail("sendMessage", 429, "Too Many Requests: retry after 3", retry_after=3)
    with pytest.raises(RetryAfter) as flood:
        await api.send_message(-100, "x")
    assert flood.value.retry_after == 3
    telegram.fail("getUpdates", 409, "Conflict: terminated by other getUpdates request")
    with pytest.raises(Conflict):
        await api.get_updates(None, 1, [])
    telegram.raw("getMe", 502, b"<html>Bad Gateway</html>")
    with pytest.raises(NetworkError):
        await api.get_me()


async def test_network_failures_never_leak_the_token():
    api = Api(TOKEN, transport=HttpTransport(retries=1, backoff=0), base_url="http://127.0.0.1:1")
    with pytest.raises(NetworkError) as refused:
        await api.get_me()
    assert TOKEN not in str(refused.value) and "getMe" in str(refused.value)


async def test_slow_server_is_a_network_error_not_a_hang(telegram):
    telegram.reply("getMe", {"id": 1}, pause=1.5)
    started = time.monotonic()
    with pytest.raises(NetworkError):
        await make_api(telegram).call("getMe", timeout=0.2)
    assert time.monotonic() - started < 1.2


async def test_thread_bridge_returns_results_and_raises_errors():
    assert await in_thread(sum, [1, 2, 3]) == 6
    with pytest.raises(ValueError):
        await in_thread(int, "не число")


async def test_cancelling_the_wait_does_not_wait_for_the_thread():
    release = threading.Event()
    task = asyncio.create_task(in_thread(release.wait, 5))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 0.5
    release.set()  # поток заканчивает тихо: результат уже никому не нужен
    await asyncio.sleep(0.05)


# --- цикл опроса ---------------------------------------------------------------------------------
class ScriptedApi(Api):
    """Отдаёт заготовленные результаты getUpdates; Exception из списка — будет брошен."""

    def __init__(self, outcomes: list[Any], webhook_error: Exception | None = None) -> None:
        super().__init__(TOKEN, transport=object())  # type: ignore[arg-type]
        self.outcomes = list(outcomes)
        self.offsets: list[int | None] = []
        self.webhook_calls = 0
        self.webhook_error = webhook_error

    async def delete_webhook(self) -> None:
        self.webhook_calls += 1
        if self.webhook_error:
            raise self.webhook_error

    async def get_updates(self, offset, timeout, allowed_updates):
        self.offsets.append(offset)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def raw_update(update_id: int, text: str = "привет") -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 1, "is_bot": False, "first_name": "А"},
            "text": text,
        },
    }


class Recorder:
    def __init__(self) -> None:
        self.handled: list[int] = []
        self.saved: list[int] = []
        self.sleeps: list[float] = []
        self.fail_on: set[int] = set()

    async def handle(self, update: Update) -> None:
        if update.update_id in self.fail_on:
            raise RuntimeError("сломался обработчик")
        self.handled.append(update.update_id)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


async def run_poll(api: ScriptedApi, rec: Recorder, **kwargs: Any) -> None:
    """Опрос идёт, пока сценарий не закончится: тогда последний вызов бросает CancelledError."""
    api.outcomes.append(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await poll_updates(
            api, rec.handle, allowed_updates=["message"], save_offset=rec.saved.append,
            sleep=rec.sleep, **kwargs,
        )  # fmt: skip


async def test_updates_are_handled_in_order_and_offset_advances():
    api, rec = ScriptedApi([[raw_update(10), raw_update(11)], []]), Recorder()
    await run_poll(api, rec)
    assert rec.handled == [10, 11]
    assert api.offsets == [None, 12, 12]  # Telegram узнаёт, что 10 и 11 получены
    assert rec.saved == [11, 12]
    assert api.webhook_calls == 1


async def test_polling_resumes_from_a_saved_offset():
    api, rec = ScriptedApi([[]]), Recorder()
    await run_poll(api, rec, offset=500)
    assert api.offsets[0] == 500


async def test_a_failing_update_is_skipped_and_polling_goes_on():
    api, rec = ScriptedApi([[raw_update(10), raw_update(11)]]), Recorder()
    rec.fail_on = {10}
    await run_poll(api, rec)
    assert rec.handled == [11]
    assert rec.saved == [11, 12]  # «ядовитое» обновление не зацикливает бота


async def test_a_broken_update_does_not_stop_polling():
    api, rec = ScriptedApi([[{"update_id": 5, "message": {"oops": 1}}, raw_update(6)]]), Recorder()
    await run_poll(api, rec)
    assert rec.handled == [6] and rec.saved == [6, 7]


async def test_conflict_waits_and_retries():
    api, rec = ScriptedApi([Conflict("Conflict: other getUpdates"), []]), Recorder()
    await run_poll(api, rec)
    assert rec.sleeps == [10.0] and len(api.offsets) == 3


async def test_flood_and_network_errors_back_off_and_recover():
    api = ScriptedApi(
        [RetryAfter("flood", retry_after=3), NetworkError("сеть"), NetworkError("сеть"),
         NetworkError("сеть"), [], NetworkError("сеть")]
    )  # fmt: skip
    rec = Recorder()
    await run_poll(api, rec)
    assert rec.sleeps == [4.0, 1.0, 2.0, 4.0, 1.0]  # после удачного опроса пауза снова 1 с


async def test_backoff_is_capped():
    api, rec = ScriptedApi([NetworkError("сеть")] * 8), Recorder()
    await run_poll(api, rec)
    assert rec.sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


async def test_a_bad_token_stops_polling_with_the_error():
    api, rec = ScriptedApi([Unauthorized("Unauthorized")]), Recorder()
    with pytest.raises(Unauthorized):
        await poll_updates(api, rec.handle, allowed_updates=["message"], sleep=rec.sleep)


async def test_webhook_reset_failure_is_tolerated_but_bad_token_is_not():
    api, rec = ScriptedApi([[]], webhook_error=NetworkError("нет сети")), Recorder()
    await run_poll(api, rec)
    assert api.webhook_calls == 1 and len(api.offsets) == 2
    api = ScriptedApi([], webhook_error=Unauthorized("Unauthorized"))
    with pytest.raises(Unauthorized):
        await poll_updates(api, rec.handle, allowed_updates=["message"], sleep=rec.sleep)


async def test_cancelled_update_is_not_marked_as_done():
    api, rec = ScriptedApi([[raw_update(10)]]), Recorder()

    async def handle(update: Update) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await poll_updates(
            api, handle, allowed_updates=["message"], save_offset=rec.saved.append, sleep=rec.sleep
        )
    assert rec.saved == []  # после перезапуска обновление придёт снова
