from conftest import FRI, FRI2, MON, THU, WED
from norm_tasker.chat.resolver import resolve
from norm_tasker.chat.rules import IntentKind, parse_message
from norm_tasker.tracker.stages import Stage


def run(tracker, text, actor, reply=None):
    intent = parse_message(text, tracker.today())
    assert intent is not None, text
    return intent, resolve(tracker, intent, actor, reply_post_ids=reply)


def dates(resolution):
    return [p.publish_date for p in resolution.posts]


# --- «беру» -------------------------------------------------------------------------------


def test_claim_by_weekday_takes_the_nearest_one(tracker, board, alpha):
    _, res = run(tracker, "беру пост на пятницу", alpha)
    assert res.status == "resolved" and dates(res) == [FRI]
    assert not res.confident  # выбирали из двух пятниц — назвать выбранную дату в ответе


def test_claim_by_exact_date_is_confident(tracker, board, alpha):
    _, res = run(tracker, "возьму на 09.10", alpha)
    assert res.status == "resolved" and dates(res) == [FRI2] and res.confident


def test_claim_of_missing_date_finds_nothing(tracker, board, alpha):
    assert run(tracker, "беру на 16.10", alpha)[1].status == "none"


def test_claim_with_rubric_and_weekday_picks_by_rubric_first(tracker, board, alpha):
    _, res = run(tracker, "беру развлекательный на пятницу", alpha)
    assert dates(res) == [FRI2]


def test_claim_by_topic_words(tracker, board, alpha):
    _, res = run(tracker, "беру мем про фильм", alpha)
    assert dates(res) == [FRI2] and not res.confident
    # Пост про суперлайк уже вышел — брать нечего.
    assert run(tracker, "беру пост про суперлайк", alpha)[1].status == "none"


def test_claim_by_number(tracker, board, alpha):
    _, res = run(tracker, f"беру №{board[MON].id}", alpha)
    assert dates(res) == [MON] and res.confident


def test_bare_claim_offers_free_posts(tracker, board, alpha, beta):
    tracker.claim(board[WED].id, beta)
    _, res = run(tracker, "беру", alpha)
    assert res.status == "ambiguous"
    assert dates(res) == [THU, FRI, MON, FRI2]  # занятый пост и вышедший не предлагаем


def test_bare_claim_with_single_free_post_is_guess(tracker, board, alpha, beta):
    for day in (WED, THU, FRI, MON):
        tracker.claim(board[day].id, beta)
    _, res = run(tracker, "беру", alpha)
    assert res.status == "resolved" and dates(res) == [FRI2] and not res.confident


def test_reply_to_bot_message(tracker, board, alpha):
    ids = [board[FRI].id, board[MON].id]
    _, res = run(tracker, "беру", alpha, reply=ids)
    assert res.status == "ambiguous" and dates(res) == [FRI, MON]
    _, res = run(tracker, "беру пятничный", alpha, reply=ids)
    assert res.status == "resolved" and dates(res) == [FRI] and res.confident
    _, res = run(tracker, "беру", alpha, reply=[board[MON].id])
    assert res.status == "resolved" and dates(res) == [MON] and res.confident


def test_claim_of_two_days_in_one_message(tracker, board, alpha):
    _, res = run(tracker, "беру пост на четверг и на пятницу", alpha)
    assert res.status == "resolved" and dates(res) == [THU, FRI]


def test_claim_prefers_free_posts_when_reference_is_vague(tracker, board, alpha, beta):
    tracker.claim(board[FRI].id, beta)
    _, res = run(tracker, "беру пост на пятницу", alpha)
    assert dates(res) == [FRI2]  # ближайшая пятница занята, значит имелась в виду вторая


# --- этапы -----------------------------------------------------------------------------------


def test_stage_message_uses_posts_of_the_author(tracker, board, alpha, beta):
    tracker.claim(board[FRI].id, alpha)
    tracker.claim(board[WED].id, beta)
    intent, res = run(tracker, "текст готов", alpha)
    assert res.status == "resolved" and dates(res) == [FRI] and not res.confident
    assert res.stages == {board[FRI].id: Stage.TEXT_SHOWN}
    assert intent.kind == IntentKind.STAGE


def test_stage_message_with_two_candidates_asks(tracker, board, alpha):
    tracker.claim(board[FRI].id, alpha)
    tracker.claim(board[MON].id, alpha)
    assert run(tracker, "текст готов", alpha)[1].status == "ambiguous"
    _, res = run(tracker, "текст готов на понедельник", alpha)
    assert dates(res) == [MON] and res.confident


def test_copywriter_without_posts_is_ignored(tracker, board, alpha):
    assert run(tracker, "текст готов", alpha)[1].status == "none"


def test_designer_handoff_by_responsible(tracker, board, alpha, lead):
    tracker.claim(board[FRI].id, alpha)
    tracker.set_stage(board[FRI].id, Stage.TEXT_OK, alpha)
    _, res = run(tracker, "отдал дизайнеру", lead)
    assert res.status == "resolved" and dates(res) == [FRI]
    # Второй пост с одобренным текстом — придётся уточнить.
    tracker.claim(board[MON].id, alpha)
    tracker.set_stage(board[MON].id, Stage.TEXT_OK, alpha)
    assert run(tracker, "отдал дизайнеру", lead)[1].status == "ambiguous"


def test_explicit_reference_ignores_current_stage(tracker, board, alpha, lead):
    _, res = run(tracker, f"дизайн готов №{board[FRI].id}", lead)
    assert res.status == "resolved" and res.confident
    _, res = run(tracker, "дизайн готов на пятницу", lead)
    assert res.status == "none"  # без явной ссылки нужен подходящий этап; пост ещё не у дизайнера


def test_published_message_picks_todays_post(tracker, board, alpha, clock):
    clock.set(2026, 9, 30, 15)
    _, res = run(tracker, "вышел", alpha)
    assert res.status == "resolved" and dates(res) == [WED]
    clock.set(2026, 9, 29, 15)  # накануне: сегодня выходить нечему, молчим
    assert run(tracker, "вышел", alpha)[1].status == "none"


def test_client_ok_depends_on_post_stage(tracker, board, alpha, lead):
    tracker.claim(board[FRI].id, alpha)
    tracker.set_stage(board[FRI].id, Stage.TEXT_SHOWN, alpha)
    _, res = run(tracker, "клиент ок", lead)
    assert res.status == "resolved" and res.stages == {board[FRI].id: Stage.TEXT_OK}
    tracker.claim(board[MON].id, alpha)
    tracker.set_stage(board[MON].id, Stage.SHOWN_DESIGN, lead)
    assert run(tracker, "клиент ок", lead)[1].status == "ambiguous"
    _, res = run(tracker, "клиент ок на понедельник", lead)
    assert res.stages == {board[MON].id: Stage.FINAL_OK}


def test_shown_to_client_depends_on_post_stage(tracker, board, alpha, lead):
    tracker.claim(board[FRI].id, alpha)
    _, res = run(tracker, "показал клиенту", alpha)
    assert res.stages == {board[FRI].id: Stage.TEXT_SHOWN}
    tracker.set_stage(board[FRI].id, Stage.AT_DESIGNER, lead)
    _, res = run(tracker, "показал клиенту", lead)
    assert res.stages == {board[FRI].id: Stage.SHOWN_DESIGN}


# --- остальное ---------------------------------------------------------------------------------


def test_release_only_touches_own_posts(tracker, board, alpha, beta, lead):
    tracker.claim(board[FRI].id, alpha)
    tracker.claim(board[WED].id, beta)
    _, res = run(tracker, "не успеваю пост на пятницу", alpha)
    assert dates(res) == [FRI]
    assert run(tracker, "не успеваю пост на среду", alpha)[1].status == "none"
    _, res = run(tracker, f"не успеваю пост №{board[WED].id}", lead)
    assert dates(res) == [WED]  # ответственный может снять пост с любого


def test_move_and_not_post_need_a_reference(tracker, board, alpha):
    intent, res = run(tracker, f"№{board[FRI].id} переносим на 20.10", alpha)
    assert intent.kind == IntentKind.MOVE and dates(res) == [FRI]
    _, res = run(tracker, "это не пост", alpha, reply=[board[THU].id])
    assert dates(res) == [THU] and res.confident
    _, res = run(tracker, "пост про фильм переносим на 20.10", alpha)
    assert dates(res) == [FRI2]


def test_cancelled_posts_are_not_resolved(tracker, board, alpha):
    tracker.cancel(board[FRI].id, alpha)
    assert run(tracker, f"беру №{board[FRI].id}", alpha)[1].status == "none"
    assert dates(run(tracker, "беру пост на пятницу", alpha)[1]) == [FRI2]
