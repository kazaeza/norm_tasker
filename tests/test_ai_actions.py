from datetime import date

from conftest import FRI, THU, WED
from norm_tasker.ai.interpreter import Action
from norm_tasker.chat.ai_actions import combine, run_actions
from norm_tasker.reply import Button, Reply
from norm_tasker.tracker.stages import Stage


def act(tracker, actor, *actions, addressed=True, text="сообщение из чата"):
    return run_actions(tracker, actor, tuple(actions), addressed=addressed, text=text)


def buttons(reply):
    return [button.data for row in reply.buttons for button in row]


def boss(tracker):
    return tracker.team.identify(104, "boss_user", "Руководитель")


# --- действия по просьбе к боту ------------------------------------------------------------


def test_claim_and_stage_are_confirmed_in_words_with_an_undo_button(tracker, board, alpha):
    post = board[FRI]
    reply = act(tracker, alpha, Action("claim", (post.id,)))
    assert "Копирайтер Альфа" in reply.text and f"№{post.id}" in reply.text
    assert not reply.react  # ИИ мог ошибиться: тихой реакции мало, человек должен видеть запись
    assert [b.startswith("undo:") for b in buttons(reply)] == [True]
    assert tracker.get(post.id).assignee_username == "cw_alpha"

    reply = act(tracker, alpha, Action("stage", (post.id,), Stage.TEXT_SHOWN))
    assert "текст у клиента" in reply.text and not reply.react
    assert tracker.get(post.id).stage == Stage.TEXT_SHOWN
    assert reply.post_ids == [post.id]


def test_journal_keeps_the_chat_source_and_the_message(tracker, board, alpha):
    post = board[FRI]
    act(tracker, alpha, Action("claim", (post.id,)), text="беру-ка пятничный")
    entry = tracker.history(post.id)[0]
    assert entry.source == "chat" and entry.text == "беру-ка пятничный"
    assert entry.actor_name == "Копирайтер Альфа" and entry.undoable


def test_several_posts_in_one_action_and_several_actions_in_one_message(tracker, board, alpha):
    wed, fri = board[WED], board[FRI]
    tracker.claim(wed.id, alpha)
    tracker.claim(fri.id, alpha)
    reply = act(tracker, alpha, Action("stage", (wed.id, fri.id), Stage.TEXT_SHOWN))
    assert f"№{wed.id}" in reply.text and f"№{fri.id}" in reply.text
    assert tracker.get(wed.id).stage == tracker.get(fri.id).stage == Stage.TEXT_SHOWN

    thu = board[THU]
    reply = act(
        tracker,
        alpha,
        Action("claim", (thu.id,)),
        Action("stage", (fri.id,), Stage.TEXT_OK),
    )
    assert tracker.get(thu.id).assignee_username == "cw_alpha"
    assert tracker.get(fri.id).stage == Stage.TEXT_OK
    assert reply.text.count("\n") >= 1 and len(reply.post_ids) == 2


def test_stage_goes_forward_only_unless_the_client_sent_it_back(tracker, board, alpha):
    post = board[FRI]
    tracker.claim(post.id, alpha)
    tracker.set_stage(post.id, Stage.TEXT_OK, alpha)
    reply = act(tracker, alpha, Action("stage", (post.id,), Stage.TEXT_SHOWN))
    assert "уже дальше" in reply.text and tracker.get(post.id).stage == Stage.TEXT_OK
    reply = act(tracker, alpha, Action("rollback", (post.id,), Stage.TAKEN))
    assert tracker.get(post.id).stage == Stage.TAKEN and reply.text


def test_move_release_and_not_post(tracker, board, alpha):
    thu = board[THU]
    tracker.claim(thu.id, alpha)
    reply = act(tracker, alpha, Action("move", (thu.id,), day=date(2026, 10, 6)))
    assert "вт 06.10" in reply.text and tracker.get(thu.id).publish_date == date(2026, 10, 6)
    reply = act(tracker, alpha, Action("release", (thu.id,)))
    assert "снова свободен" in reply.text and not tracker.get(thu.id).assigned
    reply = act(tracker, alpha, Action("not_post", (thu.id,)))
    assert "убран из трекера" in reply.text and tracker.get(thu.id).cancelled


def test_add_post(tracker, alpha):
    reply = act(tracker, alpha, Action("add_post", day=date(2026, 10, 15), topic="Срочный пост"))
    assert reply.text.startswith("➕ Добавлен пост") and "Срочный пост" in reply.text
    [post] = tracker.posts_on(date(2026, 10, 15))
    assert post.topic == "Срочный пост" and post.source == "chat" and not post.in_kp
    assert f"claim:{post.id}" in buttons(reply)


def test_absurd_dates_and_unknown_posts_are_refused(tracker, board, alpha):
    thu = board[THU]
    assert act(tracker, alpha, Action("move", (thu.id,), day=date(2030, 1, 1))) is None
    assert act(tracker, alpha, Action("add_post", day=date(2019, 1, 1), topic="x")) is None
    assert tracker.get(thu.id).publish_date == THU
    assert act(tracker, alpha, Action("claim", (9999,))) is None


def test_people_who_do_not_write_to_the_tracker_cannot_record(tracker, board):
    post = board[FRI]
    assert act(tracker, boss(tracker), Action("claim", (post.id,))) is None
    assert not tracker.get(post.id).assigned


def test_someone_elses_post_can_be_taken_only_with_a_button(tracker, board, alpha, beta):
    post = board[FRI]
    tracker.claim(post.id, alpha)
    reply = act(tracker, beta, Action("claim", (post.id,)))
    assert "уже у Копирайтер Альфа" in reply.text and f"steal:{post.id}" in buttons(reply)
    assert tracker.get(post.id).assignee_username == "cw_alpha"


# --- сообщения, которые боту не адресовали -------------------------------------------------


def test_unaddressed_message_is_recorded_when_it_is_plausible(tracker, board, alpha):
    post = board[FRI]
    reply = act(tracker, alpha, Action("claim", (post.id,)), addressed=False)
    assert reply is not None and tracker.get(post.id).assignee_username == "cw_alpha"
    reply = act(tracker, alpha, Action("stage", (post.id,), Stage.TEXT_SHOWN), addressed=False)
    assert "текст у клиента" in reply.text and tracker.get(post.id).stage == Stage.TEXT_SHOWN


def test_unaddressed_message_cannot_move_delete_or_add_posts(tracker, board, alpha):
    thu = board[THU]
    tracker.claim(thu.id, alpha)
    for action in (
        Action("move", (thu.id,), day=date(2026, 10, 6)),
        Action("not_post", (thu.id,)),
        Action("rollback", (thu.id,), Stage.NEW),
        Action("add_post", day=date(2026, 10, 15), topic="Случайный"),
    ):
        assert act(tracker, alpha, action, addressed=False) is None, action
    assert tracker.get(thu.id).publish_date == THU and not tracker.get(thu.id).cancelled
    assert tracker.posts_on(date(2026, 10, 15)) == []


def test_unaddressed_message_cannot_skip_stages_or_touch_finished_posts(tracker, board, alpha):
    post = board[FRI]
    tracker.claim(post.id, alpha)
    jump = Action("stage", (post.id,), Stage.FINAL_OK)
    assert act(tracker, alpha, jump, addressed=False) is None
    assert tracker.get(post.id).stage == Stage.TAKEN
    # Если человек прямо просит боту записать, перескок допустим: он сам за это отвечает.
    assert act(tracker, alpha, jump, addressed=True) is not None
    assert tracker.get(post.id).stage == Stage.FINAL_OK
    published = board[date(2026, 9, 28)]
    assert (
        act(tracker, alpha, Action("stage", (published.id,), Stage.TEXT_SHOWN), addressed=False)
        is None
    )


def test_unaddressed_message_does_not_touch_other_peoples_posts(tracker, board, alpha, beta, lead):
    post = board[FRI]
    tracker.claim(post.id, alpha)
    for action in (
        Action("claim", (post.id,)),  # чужой пост: перехватывать нельзя
        Action("release", (post.id,)),  # и отдавать за другого тоже
        Action("stage", (post.id,), Stage.TEXT_SHOWN),  # копирайтер отчитывается о своих постах
    ):
        assert act(tracker, beta, action, addressed=False) is None, action
    assert tracker.get(post.id).assignee_username == "cw_alpha"
    assert tracker.get(post.id).stage == Stage.TAKEN
    # А о том, что сказал клиент, может сообщить любой участник.
    tracker.set_stage(post.id, Stage.TEXT_SHOWN, alpha)
    reply = act(tracker, lead, Action("stage", (post.id,), Stage.TEXT_OK), addressed=False)
    assert reply is not None and tracker.get(post.id).stage == Stage.TEXT_OK


def test_unaddressed_repeat_of_a_report_is_not_commented(tracker, board, alpha):
    post = board[FRI]
    tracker.claim(post.id, alpha)
    tracker.set_stage(post.id, Stage.TEXT_SHOWN, alpha)
    again = Action("stage", (post.id,), Stage.TEXT_SHOWN)
    assert act(tracker, alpha, again, addressed=False) is None
    assert "уже на этапе" in act(tracker, alpha, again, addressed=True).text
    assert act(tracker, alpha, Action("claim", (post.id,)), addressed=False) is None


# --- склейка ответов -----------------------------------------------------------------------


def test_combine_replies():
    assert combine([]) is None and combine([None, Reply()]) is None
    single = Reply("Раз", [[Button("а", "1")]], post_ids=[1])
    assert combine([None, single]) is single
    both = combine([single, Reply("Два", [[Button("б", "2")]], post_ids=[2])])
    assert both.text == "Раз\nДва" and both.post_ids == [1, 2]
    assert [b.data for row in both.buttons for b in row] == ["1", "2"]
    assert both.kind == "confirm"
