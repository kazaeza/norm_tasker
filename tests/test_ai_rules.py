"""Правила команды в инструкции бота: сроки и расписание берутся из настроек."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from ai_fixture import KEY, FakeTransport, verdict
from norm_tasker.ai.assistant import (
    SYSTEM,
    Assistant,
    Request,
    build_snapshot,
    reminder_mode,
    system_prompt,
    team_rules,
)
from norm_tasker.ai.gemini import GeminiClient
from norm_tasker.config import DeadlineSettings, ScheduleSettings

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=ZoneInfo("Europe/Moscow"))
FULL_MODE = "Режим напоминаний: полный, все напоминания по расписанию включены."


def test_rules_tell_the_process_the_deadlines_and_the_owners(settings):
    rules = team_rules(settings, NOW)
    assert "КП ты только читаешь и ничего в нём не пишешь" in rules
    assert "Чат главнее таблицы" in rules
    assert "Копирайтер выбирает пост из КП и пишет в чат, что берёт его" in rules
    assert "к концу D−2 текст готов и показан клиенту (копирайтеры)" in rules
    assert "к концу D−1 готовый пост с дизайном показан клиенту (ответственный за проект)" in rules
    assert "пост должен быть взят к концу D−4" in rules
    assert "лучше получить до конца D−2, крайний срок — 13:00 в D−1" in rules
    assert "в 09:00–10:00 утра D−1, крайний срок — 13:00 по Москве" in rules
    assert "руководитель почти не вмешивается: ты не тегаешь его никогда" in rules
    assert "Конец рабочего дня — 18:00 по Москве" in rules


def test_rules_have_the_weekday_table_from_the_plan(settings):
    rules = team_rules(settings, NOW)
    assert "(* — на предыдущей неделе)" in rules
    expected = {
        "пн": ("вт*", "чт*", "пт*"),
        "вт": ("ср*", "пт*", "пн"),
        "ср": ("чт*", "пн", "вт"),
        "чт": ("пт*", "вт", "ср"),
        "пт": ("пн", "ср", "чт"),
        "сб и вс": ("вт", "чт", "пт"),
    }
    for day, (take, text, show) in expected.items():
        row = (
            f"• выход {day}: взять до конца {take}, текст клиенту до конца {text}, "
            f"дизайн и показ клиенту до конца {show}"
        )
        assert row in rules, row


def test_rules_tell_about_the_designer_and_the_kp(settings):
    rules = team_rules(settings, NOW)
    assert "его время на 2 часа впереди московского" in rules and "уходит 2–3 ч" in rules
    assert "Если к 11:00 в D−1 ока клиента по тексту нет, дизайн под угрозой" in rules
    assert "если нет и к 13:00, пост не успеет" in rules
    assert "показывают клиенту 25-го числа" in rules
    assert "в ближайший следующий рабочий день" in rules
    assert "старт объявляется за 4 рабочих дня до показа" in rules
    assert "конец рабочего дня за 1 рабочий день до показа" in rules
    assert "темы первых дней месяца стоит согласовывать заранее" in rules


def test_rules_list_everything_the_bot_does_by_itself(settings):
    rules = team_rules(settings, NOW)
    assert "Ты не ждёшь, пока к тебе обратятся" in rules
    assert "(в выходные и праздники ты молчишь)" in rules
    for line in (
        "• 09:30 — утреннее саммари одним сообщением",
        "• D−4, 16:00 — если пост никто не взял",
        "• D−2, 12:00 — авторам: до 18:00 текст должен быть готов и показан клиенту",
        "• D−2, 18:00 — просрочка: текст не показан клиенту",
        "• D−1, 11:00 — ока клиента по тексту нет",
        "• D−1, 13:00 — пост не передан дизайнеру",
        "• D−1, 16:00 — готовый пост ещё не показан клиенту",
        "• D−1, 18:00 — просрочка: готовый пост не показан клиенту",
        "• в день выхода, 12:00 — нет финального ока клиента",
        "• в последний рабочий день недели, 17:00 — отчёт за неделю",
        "напоминаешь в 12:00, а без отметки ещё раз в 18:00",
    ):
        assert line in rules, line
    # Что делает бот в первый рабочий день недели, сказано прямо.
    assert "В первый рабочий день недели (обычно понедельник)" in rules
    assert "что уже в работе" in rules and "с кнопками «Беру»" in rules
    assert "не пишешь клиенту и дизайнеру" in rules and "«напомни мне в 15:00»" in rules


def test_rules_follow_the_settings_instead_of_repeating_defaults(settings):
    changed = settings.model_copy(
        update={
            "deadlines": DeadlineSettings(text_days_before=3, text_ok_hard=time(12, 0)),
            "schedule": ScheduleSettings(summary=time(8, 45), weekly_report=time(16, 30)),
            "weekend_reminders": True,
            "designer_timezone": "Europe/Moscow",
        }
    )
    rules = team_rules(changed, NOW)
    assert "к концу D−3 текст готов и показан клиенту" in rules
    assert "крайний срок — 12:00 в D−1" in rules
    assert "• 08:45 — утреннее саммари" in rules and "09:30" not in rules
    assert "16:30 — отчёт за неделю" in rules
    assert "(в выходные и праздники ты молчишь)" not in rules  # выходные включены
    assert "его время на" not in rules  # часовые пояса совпали


def test_soft_start_paragraph_appears_only_when_there_is_a_soft_start(settings):
    assert "Мягкий старт. В первые 14 дней после запуска" in team_rules(settings, NOW)
    assert "SOFT_START_DAYS=0" in team_rules(settings, NOW)
    settings.soft_start_days = 0
    assert "Мягкий старт" not in team_rules(settings, NOW)


def test_rules_have_no_names_or_usernames_from_the_settings(settings):
    """Репозиторий публичный: имена людей приходят в «ДАННЫХ», а не живут в инструкции."""
    prompt = system_prompt(settings, NOW)
    for word in ("cw_alpha", "cw_beta", "lead_user", "boss_user", "Альфа", "Бета"):
        assert word not in prompt
    for name in [*settings.client_authors, *settings.team_authors]:
        assert name not in prompt


def test_system_prompt_is_the_role_then_the_rules_then_the_format_reminder(settings):
    prompt = system_prompt(settings, NOW)
    assert prompt.startswith(SYSTEM) and "ПРАВИЛА КОМАНДЫ" in prompt
    assert prompt.index("ПРАВИЛА КОМАНДЫ") > prompt.index("Ответь ОДНИМ JSON-объектом")
    assert prompt.rstrip().endswith("один JSON-объект, как описано в самом начале.")


def test_the_bot_no_longer_tells_people_it_cannot_remind():
    assert "(напомнить, написать клиенту)" not in SYSTEM
    assert "не говори, что не напоминаешь" in SYSTEM
    assert "«Режим напоминаний»" in SYSTEM


# --- режим напоминаний ---------------------------------------------------------------------


def test_reminder_mode_says_until_when_the_soft_start_lasts(tracker, clock):
    assert "включатся через 14 дней после запуска" in reminder_mode(tracker, tracker.now())
    tracker.state.set_meta("first_run", "2026-09-29")
    line = reminder_mode(tracker, tracker.now())
    assert line.startswith("Режим напоминаний: мягкий старт")
    assert "напоминания по срокам постов и эскалации включатся 13.10" in line
    clock.set(2026, 10, 12, 10)
    assert "включатся 13.10" in reminder_mode(tracker, tracker.now())
    clock.set(2026, 10, 13, 10)
    assert reminder_mode(tracker, tracker.now()) == FULL_MODE


def test_no_soft_start_means_the_full_mode(tracker):
    tracker.settings.soft_start_days = 0
    assert reminder_mode(tracker, tracker.now()) == FULL_MODE


def test_snapshot_carries_the_mode_and_the_nearest_kp(tracker, board, clock):
    assert "Режим напоминаний: мягкий старт" in build_snapshot(tracker, None, None)
    # 29.09: КП на октябрь показали 25.09, ноябрьское ещё не началось — про КП сказать нечего.
    assert "Ближайшее КП" not in build_snapshot(tracker, None, None)
    clock.set(2026, 10, 5, 10)
    snapshot = build_snapshot(tracker, None, None)
    assert (
        "📅 Ближайшее КП — на ноябрь: старт вт 20.10, ок ответственного — до пт 23.10, "
        "показ клиенту пн 26.10"
    ) in snapshot
    clock.set(2026, 10, 15, 10)  # скоро старт: строку даёт саммари, повтора нет
    snapshot = build_snapshot(tracker, None, None)
    assert "Ближайшее КП" not in snapshot and "📅 КП на ноябрь: старт вт 20.10" in snapshot


# --- всё вместе: что уходит в Gemini -----------------------------------------------------------


@pytest.fixture
def transport():
    return FakeTransport(verdict(kind="answer", text="Каждое утро в 09:30 пришлю саммари."))


async def test_every_question_carries_the_rules_and_the_mode(tracker, alpha, transport):
    assistant = Assistant(GeminiClient(KEY, transport=transport), tracker)
    request = Request("как часто ты будешь напоминать про задачи?", alpha, addressed=True)
    result = await assistant.interpret(request, None)
    assert result.kind == "answer" and "09:30" in result.text
    system = transport.calls[0]["payload"]["systemInstruction"]["parts"][0]["text"]
    assert system == system_prompt(tracker.settings, tracker.now())
    assert "Что ты делаешь сам" in system and "D−2, 12:00 — авторам" in system
    prompt = transport.prompt()
    assert "Режим напоминаний: мягкий старт" in prompt
    assert "ВОПРОС\nкак часто ты будешь напоминать про задачи?" in prompt
