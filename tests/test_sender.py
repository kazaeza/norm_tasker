"""Отправка сообщений: что бот делает, когда Telegram отвечает ошибкой."""

import pytest

from norm_tasker.bot.telegram import MAX_LENGTH, Sender, keyboard
from norm_tasker.reply import Button, Reply
from norm_tasker.tg.errors import BadRequest, Forbidden, NetworkError, RetryAfter
from tg_fixture import WORK_CHAT, FakeApi


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def sender(api, tracker, settings):
    return Sender(api, tracker, settings)


@pytest.fixture
def slept(monkeypatch):
    """Паузы не ждём, а записываем."""
    pauses: list[float] = []

    async def record(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr("norm_tasker.bot.telegram.asyncio.sleep", record)
    return pauses


def bad_request(text: str) -> BadRequest:
    return BadRequest(f"Bad Request: {text}", method="sendMessage", code=400)


async def test_message_goes_to_the_work_chat_with_buttons_and_no_link_preview(sender, api):
    reply = Reply("Пост <b>№5</b>", buttons=[[Button("Беру", "claim:5")], []])
    sent = await sender.send(reply, reply_to=77)
    call = api.named("SendMessage")[0]
    assert sent is not None and call["chat_id"] == WORK_CHAT
    assert call["reply_markup"] == {
        "inline_keyboard": [[{"text": "Беру", "callback_data": "claim:5"}]]
    }
    assert call["reply_parameters"]["message_id"] == 77
    assert call["link_preview_options"] == {"is_disabled": True}
    assert keyboard([[], []]) is None


async def test_nothing_to_send_or_nowhere_to_send(sender, api, settings):
    assert await sender.send(Reply()) is None
    settings.chat_id = None
    assert await sender.send(Reply("x")) is None
    assert api.calls == []


async def test_broken_markup_is_resent_as_plain_text(sender, api):
    api.fail_next("sendMessage", bad_request("can't parse entities: unexpected end tag"))
    sent = await sender.send(Reply("<b>Привет</b> &amp; пока"))
    first, second = api.named("SendMessage")
    assert first["parse_mode"] == "HTML"
    assert "parse_mode" not in second and second["text"] == "Привет & пока"
    assert sent is not None


async def test_flood_limit_is_waited_out(sender, api, slept):
    flood = RetryAfter("Too Many Requests", method="sendMessage", code=429, retry_after=3)
    api.fail_next("sendMessage", flood)
    assert await sender.send(Reply("x")) is not None
    assert slept == [4] and len(api.named("SendMessage")) == 2


async def test_missing_topic_is_dropped_and_message_goes_to_the_chat(sender, api, settings):
    settings.thread_id = 99
    api.fail_next("sendMessage", bad_request("message thread not found"))
    assert await sender.send(Reply("x")) is not None
    first, second = api.named("SendMessage")
    assert first["message_thread_id"] == 99 and "message_thread_id" not in second


async def test_bot_kicked_from_the_chat_gives_up_quietly(sender, api):
    api.fail_next("sendMessage", Forbidden("Forbidden: bot was kicked", method="sendMessage"))
    assert await sender.send(Reply("x")) is None
    assert len(api.named("SendMessage")) == 1


async def test_other_bad_requests_are_not_retried(sender, api):
    api.fail_next("sendMessage", bad_request("chat not found"))
    assert await sender.send(Reply("x")) is None
    assert len(api.named("SendMessage")) == 1


async def test_network_errors_are_retried_a_few_times_then_dropped(sender, api, slept):
    down = NetworkError("нет связи", method="sendMessage")
    api.fail_next("sendMessage", down, down, down)
    assert await sender.send(Reply("x")) is None
    assert slept == [1, 2, 4] and len(api.named("SendMessage")) == 3
    api.clear()
    api.fail_next("sendMessage", down)
    assert await sender.send(Reply("y")) is not None  # со второй попытки дошло


async def test_long_messages_are_cut_at_a_line_break(sender, api):
    text = "\n".join(f"строка {i}" for i in range(1000))
    await sender.send(Reply(text))
    sent = api.named("SendMessage")[0]["text"]
    assert len(sent) <= MAX_LENGTH and sent.endswith("\n…")


async def test_edit_treats_unchanged_text_as_success(sender, api):
    assert await sender.edit(WORK_CHAT, 5, "новый текст") is True
    api.fail_next("editMessageText", bad_request("message is not modified"))
    assert await sender.edit(WORK_CHAT, 5, "тот же текст") is True
    api.fail_next("editMessageText", bad_request("message to edit not found"))
    assert await sender.edit(WORK_CHAT, 5, "текст") is False
    api.fail_next("editMessageText", NetworkError("нет связи"))
    assert await sender.edit(WORK_CHAT, 5, "текст") is False


async def test_reaction_and_button_removal_never_raise(sender, api):
    assert await sender.react(WORK_CHAT, 5) is True
    api.reject_reactions = True
    assert await sender.react(WORK_CHAT, 5) is False
    api.fail_next("editMessageReplyMarkup", NetworkError("нет связи"))
    await sender.remove_buttons(WORK_CHAT, 5)
    assert api.named("EditMessageReplyMarkup")
