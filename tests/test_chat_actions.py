from datetime import date

from conftest import FRI, FRI2, MON, THU, WED
from norm_tasker.chat.actions import handle_callback, handle_message
from norm_tasker.tracker.stages import Stage


def say(tracker, text, actor, reply=None, **kwargs):
    return handle_message(tracker, text, actor, reply_post_ids=reply, **kwargs)


def texts(reply):
    return reply.text or ""


def button_data(reply):
    return [b.data for row in reply.buttons for b in row]


# --- диалог из плана ----------------------------------------------------------------------


def test_plan_dialogue(tracker, board, alpha, lead):
    fri = board[FRI]
    reply = say(tracker, "беру пост на пятницу", alpha)
    assert "Копирайтер Альфа" in texts(reply) and f"№{fri.id}" in texts(reply)
    assert "пт 02.10" in texts(reply)
    assert "Текст клиенту — до ср 30.09 18:00" in texts(reply)
    assert button_data(reply) == [f"undo:{tracker.last_undoable(alpha).id}"]
    assert reply.post_ids == [fri.id]
    assert tracker.get(fri.id).assignee_username == "cw_alpha"

    # Пост назван датой — хватит реакции.
    reply = say(tracker, "текст на пятницу готов, отправил клиенту", alpha)
    assert reply.react and reply.text is None
    assert tracker.get(fri.id).stage == Stage.TEXT_SHOWN

    reply = say(tracker, "по пятничному клиент ок по тексту", lead)
    assert reply.react and reply.text is None
    assert tracker.get(fri.id).stage == Stage.TEXT_OK

    reply = say(tracker, "отдал дизайнеру пост на пятницу", lead)
    assert reply.react
    assert "у дизайнера" in texts(reply)
    assert "Показать клиенту с дизайном — до чт 01.10 18:00" in texts(reply)
    assert tracker.get(fri.id).stage == Stage.AT_DESIGNER


def test_guessed_stage_is_stated_explicitly(tracker, board, alpha):
    tracker.claim(board[FRI].id, alpha)
    reply = say(tracker, "текст готов", alpha)
    assert not reply.react
    assert f"№{board[FRI].id}" in texts(reply) and "текст у клиента" in texts(reply)
    assert any(d.startswith("undo:") for d in button_data(reply))


def test_stage_message_from_someone_with_no_posts_is_ignored(tracker, board, beta):
    assert say(tracker, "текст готов", beta) is None
    assert say(tracker, "привет, как дела", beta) is None


def test_boss_is_never_answered(tracker, board, settings):
    boss = tracker.team.identify(500, "boss_user", "Руководитель")
    assert say(tracker, "беру пост на пятницу", boss) is None


# --- «беру» ------------------------------------------------------------------------------------


def test_claim_of_taken_post_offers_to_take_over(tracker, board, alpha, beta):
    tracker.claim(board[FRI].id, beta)
    reply = say(tracker, f"беру №{board[FRI].id}", alpha)
    assert "уже у Копирайтер Бета" in texts(reply)
    assert button_data(reply) == [f"steal:{board[FRI].id}"]
    result = handle_callback(tracker, f"steal:{board[FRI].id}", alpha)
    assert tracker.get(board[FRI].id).assignee_username == "cw_alpha"
    assert result.remove_buttons and "Копирайтер Альфа" in texts(result.reply)


def test_repeated_claim_says_it_is_already_yours(tracker, board, alpha):
    say(tracker, f"беру №{board[FRI].id}", alpha)
    assert "уже за вами" in texts(say(tracker, f"беру №{board[FRI].id}", alpha))


def test_claim_of_two_days(tracker, board, alpha):
    reply = say(tracker, "беру пост на четверг и на пятницу", alpha)
    assert reply.post_ids == [board[THU].id, board[FRI].id]
    assert all(tracker.get(board[d].id).assigned for d in (THU, FRI))
    assert len(reply.buttons[0]) == 2  # отменить можно каждый отдельно


def test_claim_of_missing_post_says_so(tracker, board, alpha):
    assert "Не вижу такого поста" in texts(say(tracker, "беру пост на 16.10", alpha))
    assert say(tracker, "беру", alpha).buttons  # без указания поста бот предлагает список


def test_late_claim_warns_about_overdue_text(tracker, board, alpha, clock):
    clock.set(2026, 10, 1, 12)  # чт: срок текста для пятничного поста прошёл вчера
    reply = say(tracker, f"беру №{board[FRI].id}", alpha)
    assert "Срок показа текста клиенту уже прошёл" in texts(reply)


# --- вопросы и кнопки -------------------------------------------------------------------------


def test_ambiguous_stage_message_asks_and_button_applies(tracker, board, alpha):
    tracker.claim(board[FRI].id, alpha)
    tracker.claim(board[MON].id, alpha)
    reply = say(tracker, "текст готов", alpha)
    assert texts(reply) == "Какой пост имеется в виду?"
    options = button_data(reply)
    assert len(options) == 3 and options[-1].endswith(":0")
    assert tracker.get(board[FRI].id).stage == Stage.TAKEN  # пока ничего не изменилось

    pick = next(o for o in options if o.endswith(f":{board[MON].id}"))
    stranger = handle_callback(tracker, pick, tracker.team.identify(102, "cw_beta", "Бета"))
    assert stranger.alert and tracker.get(board[MON].id).stage == Stage.TAKEN
    result = handle_callback(tracker, pick, alpha)
    assert tracker.get(board[MON].id).stage == Stage.TEXT_SHOWN
    assert tracker.get(board[FRI].id).stage == Stage.TAKEN
    assert result.edit_text and result.reply
    assert handle_callback(tracker, pick, alpha).alert  # вопрос уже закрыт


def test_pick_none_leaves_everything(tracker, board, alpha):
    tracker.claim(board[FRI].id, alpha)
    tracker.claim(board[MON].id, alpha)
    options = button_data(say(tracker, "текст готов", alpha))
    result = handle_callback(tracker, options[-1], alpha)
    assert "ничего не меняю" in result.edit_text
    assert all(tracker.get(board[d].id).stage == Stage.TAKEN for d in (FRI, MON))


def test_claim_button_from_the_list(tracker, board, alpha, beta):
    result = handle_callback(tracker, f"claim:{board[MON].id}", alpha)
    assert "Копирайтер Альфа" in texts(result.reply)
    late = handle_callback(tracker, f"claim:{board[MON].id}", beta)
    assert late.alert == "Уже взял Копирайтер Альфа" and late.reply is None


def test_stage_button(tracker, board, alpha):
    tracker.claim(board[FRI].id, alpha)
    result = handle_callback(tracker, f"stage:{board[FRI].id}:{int(Stage.TEXT_SHOWN)}", alpha)
    assert tracker.get(board[FRI].id).stage == Stage.TEXT_SHOWN
    assert "текст у клиента" in texts(result.reply)


def test_undo_buttons(tracker, board, alpha, beta, lead):
    reply = say(tracker, f"беру №{board[FRI].id}", alpha)
    undo = button_data(reply)[0]
    assert handle_callback(tracker, undo, beta).alert.startswith("Отменить может")
    assert tracker.get(board[FRI].id).assigned
    done = handle_callback(tracker, undo, alpha)
    assert done.edit_text.startswith("↩️ Отменено")
    assert not tracker.get(board[FRI].id).assigned
    assert handle_callback(tracker, undo, alpha).alert == "Это уже отменено."


def test_undo_after_later_change_is_refused(tracker, board, alpha):
    undo = button_data(say(tracker, f"беру №{board[FRI].id}", alpha))[0]
    say(tracker, "текст на пятницу готов", alpha)
    assert "сначала отмените последнее" in handle_callback(tracker, undo, alpha).alert


def test_stale_buttons_do_not_crash(tracker, alpha):
    assert handle_callback(tracker, "claim:9999", alpha).alert == "Пост не найден"
    assert handle_callback(tracker, "bogus", alpha).alert == "Кнопка устарела"
    assert handle_callback(tracker, "claim:abc", alpha).alert == "Кнопка устарела"
    assert handle_callback(tracker, "pick:77:1", alpha).alert == "Вопрос уже закрыт"


# --- назначить другому -----------------------------------------------------------------------


def test_assign_needs_confirmation_from_the_target(tracker, board, alpha, beta):
    reply = say(tracker, "@cw_beta возьми пост на пятницу", alpha)
    assert "@cw_beta" in texts(reply) and "Берёшь?" in texts(reply)
    assert not tracker.get(board[FRI].id).assigned
    ok, no = button_data(reply)[0], button_data(reply)[1]
    assert handle_callback(tracker, ok, alpha).alert == "Отвечает тот, кому предложили"
    assert handle_callback(tracker, no, beta).edit_text == "Копирайтер Бета не берёт."
    assert not tracker.get(board[FRI].id).assigned
    assert handle_callback(tracker, ok, beta).alert == "Вопрос уже закрыт"

    reply = say(tracker, "@cw_beta возьми пост на пятницу", alpha)
    handle_callback(tracker, button_data(reply)[0], beta)
    assert tracker.get(board[FRI].id).assignee_username == "cw_beta"


def test_assign_to_unknown_person_is_ignored(tracker, board, alpha):
    assert say(tracker, "Вася, возьми пост на пятницу", alpha) is None
    assert say(tracker, "@cw_alpha возьми пост на пятницу", alpha) is None  # себе — не вопрос


# --- остальные действия ---------------------------------------------------------------------


def test_release_makes_post_free_again(tracker, board, alpha, beta):
    tracker.claim(board[FRI].id, alpha)
    reply = say(tracker, "не успеваю пост на пятницу, кто возьмёт?", alpha)
    assert "снова свободен" in texts(reply)
    assert f"claim:{board[FRI].id}" in button_data(reply)
    assert not tracker.get(board[FRI].id).assigned
    handle_callback(tracker, f"claim:{board[FRI].id}", beta)
    assert tracker.get(board[FRI].id).assignee_username == "cw_beta"


def test_move_from_chat(tracker, board, alpha):
    reply = say(tracker, f"№{board[FRI2].id} переносим на 13.10", alpha)
    moved = tracker.get(board[FRI2].id)
    assert moved.publish_date == date(2026, 10, 13)
    assert "вт 13.10" in texts(reply) and "Поправьте, пожалуйста, и КП" in texts(reply)
    assert "Текст клиенту — до пт 09.10 18:00" in texts(reply)  # D−2 для вторника
    handle_callback(tracker, button_data(reply)[0], alpha)
    assert tracker.get(board[FRI2].id).publish_date == FRI2


def test_move_by_weekday_counts_from_the_posts_date(tracker, board, alpha):
    say(tracker, f"перенесли №{board[FRI].id} на понедельник", alpha)
    assert tracker.get(board[FRI].id).publish_date == MON


def test_not_post_reply_removes_the_row(tracker, board, alpha):
    reply = say(tracker, "это не пост", alpha, reply=[board[THU].id])
    assert "убран из трекера" in texts(reply)
    assert tracker.get(board[THU].id).cancelled
    handle_callback(tracker, button_data(reply)[0], alpha)
    assert not tracker.get(board[THU].id).cancelled


def test_rollback_needs_explicit_words(tracker, board, alpha, lead):
    tracker.claim(board[FRI].id, alpha)
    tracker.set_stage(board[FRI].id, Stage.TEXT_SHOWN, alpha)
    reply = say(tracker, "клиент вернул текст на пятницу на правки", lead)
    assert tracker.get(board[FRI].id).stage == Stage.TAKEN
    assert "взят, пишется текст" in texts(reply)


def test_late_handoff_warns_about_the_designer(tracker, board, alpha, lead, clock):
    tracker.claim(board[MON].id, alpha)
    tracker.set_stage(board[MON].id, Stage.TEXT_OK, alpha)
    clock.set(2026, 10, 2, 14, 30)  # пт, после 13:00; пост выходит в понедельник
    reply = say(tracker, f"отдал дизайнеру №{board[MON].id}", lead)
    assert "после 13:00" in texts(reply) and "18:00" in texts(reply)


def test_already_later_stage_is_not_a_problem(tracker, board, lead):
    tracker.set_stage(board[FRI].id, Stage.SHOWN_DESIGN, lead)
    reply = say(tracker, f"дизайн готов №{board[FRI].id}", lead)
    assert reply.react  # ничего не меняем, но и не ругаемся
    assert tracker.get(board[FRI].id).stage == Stage.SHOWN_DESIGN


# --- КП -------------------------------------------------------------------------------------------


def test_kp_phrases(tracker, board, alpha, lead, clock):
    clock.set(2026, 10, 22, 11)  # ноябрьское КП: старт 20.10, показ 26.10
    assert "сборку ведёт Копирайтер Альфа" in texts(say(tracker, "беру кп", alpha))
    refusal = say(tracker, "кп окей", alpha)
    assert "даёт ответственный" in texts(refusal) and "@lead_user" in texts(refusal)
    month = date(2026, 11, 1)
    assert tracker.state.kp_month(month)["ok_at"] is None
    assert "финальный ок записан" in texts(say(tracker, "кп окей", lead))
    assert tracker.state.kp_month(month)["ok_at"] is not None
    assert say(tracker, "показали кп клиенту", lead).react
    assert tracker.state.kp_month(month)["shown_at"] is not None


def test_kp_buttons(tracker, alpha, lead, clock):
    clock.set(2026, 10, 22, 11)
    assert handle_callback(tracker, "kp:ok:2026-11", alpha).alert
    handle_callback(tracker, "kp:own:2026-11", alpha)
    assert tracker.state.kp_month(date(2026, 11, 1))["owner_name"] == "Копирайтер Альфа"
    handle_callback(tracker, "kp:ok:2026-11", lead)
    assert tracker.state.kp_month(date(2026, 11, 1))["ok_at"] is not None


# --- восстановление после «вышел» ---------------------------------------------------------------


def test_published_message_marks_todays_post(tracker, board, alpha, clock):
    clock.set(2026, 9, 30, 15)
    reply = say(tracker, "вышел", alpha)
    assert "опубликован" in texts(reply) and f"№{board[WED].id}" in texts(reply)
    assert tracker.get(board[WED].id).stage == Stage.PUBLISHED


def test_unanchored_talk_gets_no_answer(tracker, board, alpha):
    """«Беру отпуск», «переносим созвон» — слова без даты, номера и рубрики: бот молчит."""
    assert say(tracker, "беру отпуск на неделю", alpha) is None
    assert say(tracker, "перенесём созвон на завтра", alpha) is None
    # А когда пост назван датой, но такого нет, — отвечает.
    assert "Не вижу такого поста" in texts(say(tracker, "беру пост на 16.10", alpha))
