import json
from datetime import date

import pytest

from norm_tasker.ai.interpreter import (
    MAX_ACTIONS,
    MAX_POSTS,
    Action,
    Interpretation,
    parse_action,
    parse_interpretation,
)
from norm_tasker.tracker.stages import Stage

TODAY = date(2026, 9, 29)


def parse(payload, today=TODAY):
    raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return parse_interpretation(raw, today)


def test_answer_in_json():
    result = parse({"kind": "answer", "text": "  Сегодня горит пост №12.  ", "actions": []})
    assert result == Interpretation("answer", "Сегодня горит пост №12.", False, ())


def test_json_survives_code_fence_and_introduction():
    body = '{"kind": "clarify", "text": "Какой пост: №12 или №14?"}'
    for raw in (f"```json\n{body}\n```", f"Вот ответ:\n{body}\nГотово.", f"```\n{body}\n```"):
        result = parse(raw)
        assert result.kind == "clarify" and result.text == "Какой пост: №12 или №14?", raw


def test_plain_text_is_an_answer_but_broken_json_is_not_shown_to_people():
    assert parse("Пост №12 — у клиента.") == Interpretation("answer", "Пост №12 — у клиента.")
    assert parse("   ") == Interpretation()
    # Ответ оборвался на середине: показывать людям обрывок JSON нельзя.
    assert parse('{"kind": "answer", "text": "Пост №12 у клиен') == Interpretation()
    assert parse('```json\n{"kind": "action", "actions": [') == Interpretation()


def test_action_with_confidence():
    result = parse(
        {
            "kind": "action",
            "confidence": "high",
            "actions": [{"type": "stage", "posts": [12], "stage": "at_designer"}],
        }
    )
    assert result.kind == "action" and result.confident
    assert result.actions == (Action("stage", (12,), Stage.AT_DESIGNER),)
    assert not parse({"kind": "action", "confidence": "low", "actions": [
        {"type": "claim", "posts": [3]}]}).confident  # fmt: skip
    assert not parse({"kind": "action", "actions": [{"type": "claim", "posts": [3]}]}).confident


def test_missing_or_odd_kind_is_guessed_from_the_content():
    assert parse({"actions": [{"type": "claim", "posts": [1]}]}).kind == "action"
    assert parse({"text": "Привет!"}).kind == "answer"
    assert parse({"kind": "что-то", "text": "Привет!"}).kind == "answer"
    assert parse({}).kind == "ignore"
    # «action» без единого годного действия — уже не действие.
    assert parse({"kind": "action", "text": "Записал"}).kind == "answer"
    assert parse({"kind": "action", "actions": [{"type": "dance", "posts": [1]}]}).kind == "ignore"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"type": "claim", "posts": [12]}, Action("claim", (12,))),
        ({"type": "claim", "posts": "12"}, Action("claim", (12,))),
        ({"type": "claim", "post": "№12"}, Action("claim", (12,))),
        ({"type": "CLAIM", "posts": ["№12", "14", 12]}, Action("claim", (12, 14))),
        ({"type": "release", "posts": [5]}, Action("release", (5,))),
        ({"type": "stage", "posts": [5], "stage": "TEXT_OK"}, Action("stage", (5,), Stage.TEXT_OK)),
        (
            {"type": "rollback", "posts": [5], "stage": "taken"},
            Action("rollback", (5,), Stage.TAKEN),
        ),
        (
            {"type": "move", "posts": [5], "date": "2026-10-20"},
            Action("move", (5,), day=date(2026, 10, 20)),
        ),
        (
            {"type": "move", "posts": [5], "date": "20.10"},
            Action("move", (5,), day=date(2026, 10, 20)),
        ),
        ({"type": "not_post", "posts": [5]}, Action("not_post", (5,))),
        (
            {"type": "add_post", "date": "2026-10-15", "topic": "  Срочный   пост "},
            Action("add_post", day=date(2026, 10, 15), topic="Срочный пост"),
        ),
        # Неполные и выдуманные действия отбрасываются.
        ({"type": "claim"}, None),
        ({"type": "claim", "posts": []}, None),
        ({"type": "claim", "posts": ["без номера"]}, None),
        ({"type": "stage", "posts": [5]}, None),
        ({"type": "stage", "posts": [5], "stage": "new"}, None),
        ({"type": "stage", "posts": [5], "stage": "почти готово"}, None),
        ({"type": "move", "posts": [5]}, None),
        ({"type": "move", "posts": [5], "date": "завтра"}, None),
        ({"type": "move", "posts": [5], "date": "31.02"}, None),
        ({"type": "add_post", "topic": "без даты"}, None),
        ({"type": "delete_everything", "posts": [1]}, None),
        ({"posts": [1]}, None),
        ("claim 12", None),
        (None, None),
    ],
)
def test_parse_action(raw, expected):
    assert parse_action(raw, TODAY) == expected


def test_dates_without_a_year_take_the_nearest_sensible_year():
    def move(text, today):
        action = parse_action({"type": "move", "posts": [1], "date": text}, today)
        return action.day if action else None

    assert move("20.10", TODAY) == date(2026, 10, 20)
    assert move("01.09", TODAY) == date(2026, 9, 1)  # недавнее прошлое — этот же год
    assert move("05.01", date(2026, 12, 20)) == date(2027, 1, 5)  # январь после декабря
    assert move("05.01.27", TODAY) == date(2027, 1, 5)
    assert move("2027-01-05", TODAY) == date(2027, 1, 5)


def test_limits_on_actions_and_posts():
    many_posts = {"type": "claim", "posts": list(range(1, 30))}
    assert len(parse_action(many_posts, TODAY).posts) == MAX_POSTS
    result = parse(
        {"kind": "action", "actions": [{"type": "claim", "posts": [n]} for n in range(20)]}
    )
    assert len(result.actions) == MAX_ACTIONS
