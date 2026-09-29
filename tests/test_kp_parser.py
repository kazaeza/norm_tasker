from datetime import date

import pytest

from kp_fixture import Cell, Comment, Week, add_comments, build_kp, standard_kp
from norm_tasker.kp.parser import looks_like_note, month_of_title, month_title, parse_kp


@pytest.fixture(scope="module")
def parsed():
    return parse_kp(build_kp(standard_kp()))


def by_key(result):
    return {s.key: s for s in result.slots}


def test_month_titles():
    assert month_of_title("Октябрь 2026") == date(2026, 10, 1)
    assert month_of_title("март 2027") == date(2027, 3, 1)
    assert month_of_title("Статистика") is None
    assert month_of_title("Чекпойнты") is None
    assert month_title(date(2026, 11, 1)) == "Ноябрь 2026"


def test_basic_block_is_parsed(parsed):
    slots = by_key(parsed)
    post = slots[(date(2026, 9, 14), 1)]
    assert post.rubric == "Продукт"
    assert post.status == "Выпущено"
    assert post.topic == "Тема про поиск"  # пометка «[док]» убрана
    assert post.doc_id == "docA"
    assert post.cell == "A5"
    assert slots[(date(2026, 9, 15), 1)].doc_url is None


def test_newer_sheet_wins_for_shared_week(parsed):
    slots = by_key(parsed)
    assert slots[(date(2026, 9, 28), 1)].topic == "Новая тема A"
    assert slots[(date(2026, 9, 28), 1)].sheet == "Октябрь 2026"
    assert slots[(date(2026, 9, 29), 1)].topic == "Тема B"
    # Суббота есть только в устаревшем листе: команда убрала пост, значит его нет.
    assert (date(2026, 10, 3), 1) not in slots
    assert all("Старая" not in (s.topic or "") for s in parsed.slots)


def test_shared_week_falls_back_when_newer_block_is_empty():
    old = [Week(date(2026, 10, 26), [Cell("Продукт", None, "Тема из октября")] + [None] * 6)]
    new = [Week(date(2026, 10, 26), [Cell("Продукт")] + [None] * 6)]  # только рубрики
    result = parse_kp(build_kp({"Октябрь 2026": old, "Ноябрь 2026": new}))
    assert by_key(result)[(date(2026, 10, 26), 1)].topic == "Тема из октября"
    assert by_key(result)[(date(2026, 10, 26), 1)].sheet == "Октябрь 2026"


def test_rubric_only_day_is_a_post_without_topic(parsed):
    slot = by_key(parsed)[(date(2026, 9, 30), 1)]
    assert slot.topic is None
    assert slot.rubric == "Информационный"


def test_second_post_and_notes(parsed):
    slots = by_key(parsed)
    assert slots[(date(2026, 9, 28), 2)].topic == "Пост про второй повод"
    assert slots[(date(2026, 9, 28), 2)].rubric is None
    assert slots[(date(2026, 9, 28), 2)].status is None
    # Заметка с пометкой NB и текст в дне без основного поста — не посты.
    assert (date(2026, 10, 2), 2) not in slots
    assert (date(2026, 10, 4), 1) not in slots
    assert (date(2026, 10, 4), 2) not in slots
    assert looks_like_note("NB: важно")
    assert not looks_like_note("Пост про факт")


def test_statuses_are_kept_raw_and_unknown_ones_reported(parsed):
    slots = by_key(parsed)
    assert slots[(date(2026, 9, 29), 1)].status == "В работе"  # пробел в конце срезан
    assert parsed.unknown_statuses == {"Странный статус"}
    assert any("Странный статус" in w for w in parsed.warnings)


def test_only_calendar_sheets_and_calendar_days(parsed):
    assert {s.name for s in parsed.sheets} == {"Сентябрь 2026", "Октябрь 2026"}
    october = next(s for s in parsed.sheets if s.name == "Октябрь 2026")
    september = next(s for s in parsed.sheets if s.name == "Сентябрь 2026")
    assert (october.weeks, october.used_weeks) == (2, 2)
    assert (september.weeks, september.used_weeks) == (2, 1)  # общая неделя уступила
    assert parsed.calendar_days[date(2026, 10, 10)] == "Октябрь 2026"  # день без постов тоже есть


def test_sheet_filter_by_month():
    data = build_kp(standard_kp())
    # Лист предыдущего месяца читаем всегда: в нём может оказаться начало недели на стыке.
    both = parse_kp(data, since=date(2026, 10, 20))
    assert {s.name for s in both.sheets} == {"Сентябрь 2026", "Октябрь 2026"}
    only_october = parse_kp(data, since=date(2026, 11, 20))
    assert {s.name for s in only_october.sheets} == {"Октябрь 2026"}
    only_sept = parse_kp(data, until=date(2026, 9, 30))
    assert {s.name for s in only_sept.sheets} == {"Сентябрь 2026"}


def test_unparseable_calendar_sheet_is_reported():
    data = build_kp({"Октябрь 2026": []})
    result = parse_kp(data)
    assert result.slots == []
    assert any("шаблон" in w for w in result.warnings)


def test_comments_are_linked_to_days_and_slots():
    comments = [
        Comment(
            "Октябрь 2026", "A5", "c1", "Клиент Один", "поменяйте тему", "2026-09-21T09:00:00.00"
        ),
        Comment("Октябрь 2026", "A6", "c2", "Клиент Один", "и второй пост тоже"),
        Comment("Октябрь 2026", "A5", "c3", "Копирайтер", "ответ", parent="c1"),
        Comment("Октябрь 2026", "B5", "c4", "Клиент Два", "ок", done=True),
        Comment("Октябрь 2026", "H5", "c5", "Клиент Один", "заметка сбоку"),
        Comment("Сентябрь 2026", "A10", "c6", "Клиент Один", "к устаревшему блоку"),
    ]
    result = parse_kp(add_comments(build_kp(standard_kp()), comments))
    got = {c.id: c for c in result.comments}
    assert (got["c1"].day, got["c1"].slot) == (date(2026, 9, 28), 1)
    assert got["c1"].author == "Клиент Один"
    assert got["c1"].created.isoformat() == "2026-09-21T09:00:00"
    assert (got["c2"].day, got["c2"].slot) == (date(2026, 9, 28), 2)
    assert got["c3"].thread_id == "c1" and got["c3"].day == date(2026, 9, 28)
    assert got["c4"].resolved
    assert got["c5"].day is None  # ячейка вне календаря
    # Комментарий в устаревшем блоке привязывается к тому же дню.
    assert (got["c6"].day, got["c6"].slot) == (date(2026, 9, 28), 1)


def test_reply_inherits_resolved_state_of_thread():
    comments = [
        Comment("Октябрь 2026", "A5", "root", "Клиент", "вопрос", done=True),
        Comment("Октябрь 2026", "A5", "reply", "Копирайтер", "ответ", parent="root"),
    ]
    result = parse_kp(add_comments(build_kp(standard_kp()), comments))
    assert all(c.resolved for c in result.comments)


def test_file_without_comments_and_broken_files():
    assert parse_kp(build_kp(standard_kp())).comments == []
    with pytest.raises(Exception):  # noqa: B017 — битый файл сообщает о себе исключением
        parse_kp(b"not an xlsx")
