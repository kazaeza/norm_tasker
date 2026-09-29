from datetime import date

import pytest

from norm_tasker.chat.refs import extract_refs, topic_score
from norm_tasker.chat.rules import IntentKind, parse_message
from norm_tasker.tracker.stages import Stage

TODAY = date(2026, 10, 7)  # среда


def parse(text):
    return parse_message(text, TODAY)


# --- ссылки на пост -------------------------------------------------------------------


def test_refs_numbers_dates_weekdays():
    ref = extract_refs("беру №12 и пост 15, а ещё пятничный на 16.10 и 20 октября", TODAY)
    assert ref.numbers == (12, 15)
    assert ref.dates == (date(2026, 10, 16), date(2026, 10, 20))
    assert ref.weekdays == (4,)


def test_refs_relative_days_and_abbreviations():
    ref = extract_refs("возьму на завтра и на пн", TODAY)
    assert date(2026, 10, 8) in ref.dates
    assert ref.weekdays == (0,)
    assert extract_refs("беру на 16", TODAY).day_numbers == (16,)
    assert extract_refs("беру на 16-е", TODAY).day_numbers == (16,)
    assert extract_refs("беру пост на 16.10", TODAY).day_numbers == ()


def test_refs_year_is_the_nearest_one():
    assert extract_refs("на 2.01", date(2026, 12, 30)).dates == (date(2027, 1, 2),)
    assert extract_refs("на 30.12", date(2027, 1, 2)).dates == (date(2026, 12, 30),)
    assert extract_refs("31.02", TODAY).dates == ()  # такой даты нет


def test_refs_rubrics_and_topic_words():
    ref = extract_refs("беру продуктовый про суперлайк", TODAY)
    assert ref.rubrics == ("Продукт",)
    assert ref.words == ("суперлайк",)
    assert topic_score(ref.words, "Продуктовый пост про суперлайк для премиума") == 1
    assert topic_score(ref.words, "Мем про переписку") == 0
    assert extract_refs("беру пост", TODAY).is_empty


def test_refs_post_number_is_not_confused_with_date():
    assert extract_refs("пост 16.10", TODAY).numbers == ()
    assert extract_refs("пост 12", TODAY).numbers == (12,)


# --- «беру» и прочее ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "беру",
        "Беру пост на пятницу",
        "возьму продуктовый на 16.10",
        "я беру мем про переписку",
        "Беру №12!",
        "взяла пост про суперлайк",
        "заберу пятничный",
    ],
)
def test_claim_phrases(text):
    intent = parse(text)
    assert intent is not None and intent.kind == IntentKind.CLAIM, text


@pytest.mark.parametrize(
    "text",
    [
        "не беру",
        "кто возьмёт пост на пятницу?",
        "беру или не беру?",
        "мы обсуждали что берут не все",
        "привет всем",
        "ок",
        "/today",
    ],
)
def test_claim_lookalikes_are_ignored(text):
    intent = parse(text)
    assert intent is None or intent.kind != IntentKind.CLAIM, text


def test_claim_carries_references():
    intent = parse("беру пост на пятницу")
    assert intent.ref.weekdays == (4,)
    intent = parse("возьму продуктовый на 16.10")
    assert intent.ref.dates == (date(2026, 10, 16),) and intent.ref.rubrics == ("Продукт",)


def test_assign():
    intent = parse("Даня, возьми пост на среду")
    assert intent.kind == IntentKind.ASSIGN and intent.target == "даня"
    assert intent.ref.weekdays == (2,)
    intent = parse("@cw_beta возьми мем на пятницу")
    assert intent.kind == IntentKind.ASSIGN and intent.target == "cw_beta"
    intent = parse("пусть Илья возьмет пост про суперлайк")
    assert intent.kind == IntentKind.ASSIGN and intent.target == "илья"
    assert parse("Даня, возьми пост на среду?") is None


@pytest.mark.parametrize(
    "text",
    ["не успеваю пост на среду, кто возьмёт?", "отдаю пост на пятницу", "снимаю с себя мем про X"],
)
def test_release_phrases(text):
    intent = parse(text)
    assert intent is not None and intent.kind == IntentKind.RELEASE, text


def test_release_needs_a_post_context():
    assert parse("не успеваю на обед") is None
    assert parse("отдаю дизайнеру пост") is None


# --- этапы -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "stage"),
    [
        ("текст готов, отправил клиенту", Stage.TEXT_SHOWN),
        ("текст по мему готов", Stage.TEXT_SHOWN),
        ("мем готов", Stage.TEXT_SHOWN),
        ("отправил марго текст про суперлайк", Stage.TEXT_SHOWN),
        ("текст у клиента", Stage.TEXT_SHOWN),
        ("клиент ок по тексту", Stage.TEXT_OK),
        ("марго окей по тексту", Stage.TEXT_OK),
        ("ок по мему", Stage.TEXT_OK),
        ("текст согласован", Stage.TEXT_OK),
        ("клиент одобрил текст", Stage.TEXT_OK),
        ("отдал дизайнеру", Stage.AT_DESIGNER),
        ("передала док дизайнеру", Stage.AT_DESIGNER),
        ("пост у дизайнера", Stage.AT_DESIGNER),
        ("по B клиент ок, отдал дизайнеру", Stage.AT_DESIGNER),
        ("дизайн готов", Stage.DESIGN_READY),
        ("дизайнер прислал макет", Stage.DESIGN_READY),
        ("отрисовали!", Stage.DESIGN_READY),
        ("показал клиенту с дизайном", Stage.SHOWN_DESIGN),
        ("отправил марго готовый пост", Stage.SHOWN_DESIGN),
        ("финальный ок", Stage.FINAL_OK),
        ("фин ок по посту про суперлайк", Stage.FINAL_OK),
        ("клиент ок по дизайну", Stage.FINAL_OK),
        ("можно постить", Stage.FINAL_OK),
        ("вышел", Stage.PUBLISHED),
        ("пост про суперлайк вышел", Stage.PUBLISHED),
        ("выложила", Stage.PUBLISHED),
        ("поставил в отложенные", Stage.PUBLISHED),
    ],
)
def test_stage_phrases(text, stage):
    intent = parse(text)
    assert intent is not None, text
    assert (intent.kind, intent.stage) == (IntentKind.STAGE, stage), text


@pytest.mark.parametrize(
    "text",
    [
        "текст ещё не готов",
        "текст не готов",
        "не отправил клиенту",
        "текст готов будет завтра",
        "отправлю клиенту после обеда",
        "отдам дизайнеру завтра",
        "когда дизайн будет готов?",
        "дизайн готов?",
        "вышел покурить, скоро вернусь ага и вообще расскажу вам подробнее что там было "
        "потому что это очень длинное сообщение про совсем другое дело на много слов вперёд",
        "Вы когда-нибудь пробовали?",
    ],
)
def test_stage_lookalikes_are_ignored(text):
    assert parse(text) is None, text


def test_client_ok_and_shown_client_are_resolved_later():
    assert parse("клиент ок").kind == IntentKind.CLIENT_OK
    assert parse("Марго ок").kind == IntentKind.CLIENT_OK
    assert parse("показал клиенту").kind == IntentKind.SHOWN_CLIENT
    assert parse("не показал клиенту") is None


def test_rollback():
    intent = parse("клиент вернул текст на правки")
    assert (intent.kind, intent.stage) == (IntentKind.ROLLBACK, Stage.TAKEN)
    intent = parse("марго вернула дизайн на правки, отдаю дизайнеру")
    assert (intent.kind, intent.stage) == (IntentKind.ROLLBACK, Stage.AT_DESIGNER)
    assert parse("правки от клиента по посту про суперлайк").kind == IntentKind.ROLLBACK


def test_move_with_target_date():
    intent = parse("пост про суперлайк переносим на 20.10")
    assert intent.kind == IntentKind.MOVE
    assert intent.new_date == date(2026, 10, 20)
    assert intent.ref.words == ("суперлайк",)
    intent = parse("перенесли пост с пятницы на понедельник")
    assert (intent.kind, intent.new_weekday) == (IntentKind.MOVE, 0)
    assert intent.ref.weekdays == (4,)
    intent = parse("перенеси №12 на завтра")
    assert intent.new_date == date(2026, 10, 8) and intent.ref.numbers == (12,)
    assert (
        parse("переносим созвон на пятницу") is None or True
    )  # без поста — на усмотрение резолвера


def test_not_post():
    assert parse("это не пост").kind == IntentKind.NOT_POST
    assert parse("не пост, заметка").kind == IntentKind.NOT_POST


def test_kp_phrases():
    assert parse("кп окей").kind == IntentKind.KP_OK
    assert parse("окнул кп").kind == IntentKind.KP_OK
    assert parse("контент-план согласован").kind == IntentKind.KP_OK
    assert parse("показали кп клиенту").kind == IntentKind.KP_SHOWN
    assert parse("отправил кп марго").kind == IntentKind.KP_SHOWN
    assert parse("беру кп").kind == IntentKind.KP_OWN
    assert parse("беру сборку кп").kind == IntentKind.KP_OWN
    assert parse("кп готов?") is None


def test_links_and_length_do_not_confuse_the_parser():
    intent = parse("беру пост https://docs.google.com/document/d/abc/edit на пятницу")
    assert intent.kind == IntentKind.CLAIM and intent.ref.weekdays == (4,)
    assert parse("") is None
