"""Бот целиком: настоящий HTTP до подставного сервера Telegram, запуск циклов, перезапуск."""

import asyncio
import contextlib
import time

import pytest

from norm_tasker.__main__ import main
from norm_tasker.bot.app import App
from norm_tasker.config import Env
from norm_tasker.tg.api import Api, HttpTransport
from norm_tasker.tg.errors import Unauthorized
from norm_tasker.tracker.db import Database
from norm_tasker.tracker.service import Tracker
from tg_fixture import BOT_ID, WORK_CHAT, message_json

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


@pytest.fixture(autouse=True)
def no_calendar_download(monkeypatch):
    monkeypatch.setattr("norm_tasker.bot.app.refresh_calendar", lambda *args, **kwargs: None)


@pytest.fixture
def make_app(tmp_path, settings, calendar, clock, telegram):
    """Собирает бота, который ходит в подставной Telegram по настоящему HTTP."""

    def build() -> App:
        tracker = Tracker(Database(tmp_path / "tracker.db"), settings, calendar, clock)
        tracker.state.set_meta("first_run", "2026-09-01")
        env = Env(
            bot_token=TOKEN,
            data_dir=tmp_path,
            config_path=tmp_path / "config.yaml",
            google_credentials=None,
            kp_spreadsheet_id=None,
            tracker_spreadsheet_id=None,
        )
        api = Api(TOKEN, transport=HttpTransport(retries=0, backoff=0), base_url=telegram.url)
        app = App(env, settings, api, tracker)
        app.delay_scale = 0
        return app

    return build


async def wait_for(check, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("не дождались ожидаемого")


async def run_until(app: App, check) -> None:
    running = asyncio.create_task(app.run(handle_signals=False))
    try:
        await wait_for(check)
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running


def handled(telegram, update_id: int) -> bool:
    """Ответ отправлен и Telegram уже подтверждён: следующий опрос идёт с offset за ним."""
    confirmed = any(body.get("offset") == update_id + 1 for body in telegram.bodies("getUpdates"))
    return bool(replies_to(telegram, update_id)) and confirmed


def replies_to(telegram, message_id: int) -> list[dict]:
    return [
        body
        for body in telegram.bodies("sendMessage")
        if (body.get("reply_parameters") or {}).get("message_id") == message_id
    ]


async def test_bot_answers_over_http_and_resumes_after_a_restart(make_app, telegram):
    telegram.push_update(message_json(700, "/help"))
    first = make_app()
    await run_until(first, lambda: handled(telegram, 700))

    answer = replies_to(telegram, 700)[0]
    assert answer["chat_id"] == WORK_CHAT and answer["parse_mode"] == "HTML"
    polls = [body.get("offset") for body in telegram.bodies("getUpdates")]
    assert polls[0] is None and 701 in polls  # прочитанное подтверждено
    assert first.tracker.state.get_meta("tg_offset") == f"{BOT_ID}:701"

    # Перезапуск: Telegram «забыл» бы подтверждение, но бот помнит позицию сам.
    telegram.requests.clear()
    telegram.push_update(message_json(701, "/status"))
    second = make_app()
    await run_until(second, lambda: handled(telegram, 701))
    assert telegram.bodies("getUpdates")[0]["offset"] == 701
    assert replies_to(telegram, 700) == []  # /help второй раз не разбирали


async def test_a_different_bot_starts_from_scratch(make_app, telegram):
    telegram.push_update(message_json(700, "/help"))
    app = make_app()
    app.tracker.state.set_meta("tg_offset", "555:9999")  # позиция другого бота
    await run_until(app, lambda: handled(telegram, 700))


async def test_startup_waits_until_telegram_is_reachable(make_app, telegram, monkeypatch):
    telegram.raw("getMe", 502, b"<html>bad gateway</html>")
    telegram.raw("getMe", 502, b"")
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def quick_sleep(delay: float) -> None:
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("norm_tasker.bot.app.asyncio.sleep", quick_sleep)
    app = make_app()
    await app.startup()
    assert app.bot_id == BOT_ID and app.bot_username == "norm_test_bot"
    assert slept[:2] == [1.0, 2.0]


async def test_a_wrong_token_is_reported_not_retried_forever(make_app, telegram):
    telegram.fail("getMe", 401, "Unauthorized")
    with pytest.raises(Unauthorized):
        await make_app().startup()


def test_running_with_a_wrong_token_says_what_to_check(tmp_path, telegram, monkeypatch, capsys):
    telegram.fail("getMe", 401, "Unauthorized")
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_API_URL", telegram.url)
    monkeypatch.setenv("CONFIG_YAML", f"chat_id: {WORK_CHAT}\n")
    monkeypatch.delenv("KP_SPREADSHEET_ID", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert main(["run"]) == 1
    assert "BOT_TOKEN" in capsys.readouterr().err
