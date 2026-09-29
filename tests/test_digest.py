from datetime import date

import pytest

from conftest import FRI, FRI2, MON, MON_PAST, THU, WED, make_slot
from norm_tasker.digest.board import build_board
from norm_tasker.digest.issues import post_issue
from norm_tasker.digest.kp import kp_status, summary_line
from norm_tasker.digest.reminders import RULES, due_rules, mark_done, soft_mode
from norm_tasker.digest.report import build_weekly_report, stage_times
from norm_tasker.digest.summary import build_summary, first_workday_of_week
from norm_tasker.kp.models import KpParseResult, SheetInfo
from norm_tasker.tracker.stages import Stage
from norm_tasker.tracker.sync import sync_kp


@pytest.fixture
def started(tracker):
    """Мягкий старт закончился: напоминания работают в полную силу."""
    tracker.state.set_meta("first_run", "2026-09-01")
    return tracker


def issue_of(tracker, post):
    return post_issue(tracker, tracker.get(post.id), tracker.now())


# --- что горит ----------------------------------------------------------------------------------


def test_unowned_posts_by_phase(tracker, board):
    # Сегодня вторник 29.09, 10:00.
    assert issue_of(tracker, board[WED]).code == "show_blocked"  # D−1 сегодня, никто не взял
    assert issue_of(tracker, board[THU]).code == "text_no_owner"  # D−2 сегодня
    assert issue_of(tracker, board[FRI]).code == "take_late"  # D−4 был вчера
    assert issue_of(tracker, board[MON]).code == "take_due"  # D−4 сегодня
    assert issue_of(tracker, board[MON]).level == "yellow"
    assert issue_of(tracker, board[FRI2]) is None  # рано
    assert issue_of(tracker, board[MON_PAST]) is None  # уже вышел


def test_text_phase(tracker, board, alpha, clock):
    tracker.claim(board[THU].id, alpha)  # D−2 сегодня: текст к 18:00
    issue = issue_of(tracker, board[THU])
    assert (issue.code, issue.level) == ("text_due", "yellow")
    clock.set(2026, 9, 29, 18, 30)
    issue = issue_of(tracker, board[THU])
    assert (issue.code, issue.level) == ("text_late", "red")
    tracker.set_stage(board[THU].id, Stage.TEXT_SHOWN, alpha)
    assert issue_of(tracker, board[THU]) is None  # ждём клиента — вмешиваться рано


def test_waiting_for_text_approval_escalates_through_d_minus_1(tracker, board, alpha, clock):
    tracker.claim(board[WED].id, alpha)
    tracker.set_stage(board[WED].id, Stage.TEXT_SHOWN, alpha)  # D−1 сегодня
    clock.set(2026, 9, 29, 9, 30)
    issue = issue_of(tracker, board[WED])
    assert (issue.code, issue.level) == ("ok_wait", "yellow")
    assert "до 13:00" in issue.text
    clock.set(2026, 9, 29, 11, 30)
    issue = issue_of(tracker, board[WED])
    assert (issue.code, issue.level) == ("ok_wait", "red") and "напомнить клиенту" in issue.text
    clock.set(2026, 9, 29, 13, 5)
    issue = issue_of(tracker, board[WED])
    assert (issue.code, issue.level) == ("ok_late", "red") and "показать без дизайна" in issue.text


def test_handoff_and_design_phases(tracker, board, alpha, lead, clock):
    tracker.claim(board[WED].id, alpha)
    tracker.set_stage(board[WED].id, Stage.TEXT_OK, alpha)
    clock.set(2026, 9, 29, 9, 30)
    assert "передать дизайнеру до 10:00" in issue_of(tracker, board[WED]).text
    clock.set(2026, 9, 29, 12, 0)
    assert issue_of(tracker, board[WED]).level == "yellow"
    clock.set(2026, 9, 29, 13, 30)
    assert issue_of(tracker, board[WED]).code == "handoff_late"
    tracker.set_stage(board[WED].id, Stage.AT_DESIGNER, lead)
    assert issue_of(tracker, board[WED]).code == "show_due"
    clock.set(2026, 9, 29, 18, 30)
    assert issue_of(tracker, board[WED]).code == "show_late"
    tracker.set_stage(board[WED].id, Stage.SHOWN_DESIGN, lead)
    assert issue_of(tracker, board[WED]) is None


def test_publish_day_and_after(tracker, board, alpha, lead, clock):
    tracker.set_stage(board[WED].id, Stage.SHOWN_DESIGN, lead)
    clock.set(2026, 9, 30, 10)
    assert issue_of(tracker, board[WED]).level == "yellow"
    clock.set(2026, 9, 30, 12, 30)
    assert issue_of(tracker, board[WED]).level == "red"
    tracker.set_stage(board[WED].id, Stage.FINAL_OK, lead)
    assert issue_of(tracker, board[WED]).code == "publish"
    clock.set(2026, 10, 1, 9)
    issue = issue_of(tracker, board[WED])
    assert issue.code == "unmarked" and issue.level == "yellow"
    tracker.set_stage(board[WED].id, Stage.PUBLISHED, alpha)
    assert issue_of(tracker, board[WED]) is None


def test_kp_in_work_without_owner_is_flagged(tracker, clock):
    sync_kp(tracker, [make_slot(MON, status="В работе")])
    clock.set(2026, 9, 30, 10)
    post = tracker._select()[0]
    assert issue_of(tracker, post).code == "no_owner"


# --- саммари ------------------------------------------------------------------------------------


def test_summary_sections(tracker, board, alpha, beta, lead, clock):
    tracker.claim(board[WED].id, alpha)
    tracker.set_stage(board[WED].id, Stage.TEXT_SHOWN, alpha)
    tracker.claim(board[THU].id, beta)
    tracker.claim(board[FRI].id, alpha)
    tracker.claim(board[MON].id, beta)
    clock.set(2026, 9, 29, 9, 30)
    reply, comment_ids = build_summary(tracker, tracker.now())
    text = reply.text
    assert text.startswith("☀️ Вторник, 29 сентября")
    assert "🟡 Сегодня и скоро" in text and "🔴" not in text
    assert f"№{board[WED].id}" in text and "ока по тексту пока нет" in text
    assert "🎨 Дизайнеру сегодня" in text and "как только придёт ок по тексту" in text
    assert comment_ids == []
    clock.set(2026, 9, 29, 11, 30)
    text = build_summary(tracker, tracker.now())[0].text
    assert "🔴 Горит" in text and "@lead_user" in text  # красное — с тегом ответственного


def test_summary_of_a_quiet_day(tracker):
    text = build_summary(tracker, tracker.now())[0].text
    assert "🟢 Срочного нет." in text


def test_soft_summary_has_no_tags(tracker, board, clock):
    clock.set(2026, 9, 29, 11, 30)
    assert "@lead_user" in build_summary(tracker, tracker.now())[0].text
    assert "@lead_user" not in build_summary(tracker, tracker.now(), soft=True)[0].text


def test_monday_lists_free_posts_with_buttons(tracker, board, clock):
    clock.set(2026, 10, 5, 9, 30)
    assert first_workday_of_week(tracker, tracker.today())
    reply, _ = build_summary(tracker, tracker.now())
    assert "🆓 Без ответственного на 2 недели" in reply.text
    data = [b.data for row in reply.buttons for b in row]
    assert f"claim:{board[MON].id}" in data and f"claim:{board[FRI2].id}" in data
    clock.set(2026, 10, 6, 9, 30)
    assert build_summary(tracker, tracker.now())[0].buttons == []  # не понедельник


def test_first_workday_when_monday_is_a_holiday(tracker):
    assert first_workday_of_week(tracker, date(2026, 10, 5))  # обычный понедельник
    assert not first_workday_of_week(tracker, date(2026, 10, 6))
    assert not first_workday_of_week(tracker, date(2026, 5, 11))  # выходной, не рабочий день
    assert first_workday_of_week(tracker, date(2026, 5, 12))  # вторник после выходного понедельника
    assert first_workday_of_week(tracker, date(2026, 1, 12))  # после новогодних каникул


def test_design_load_warning(tracker, board, alpha, clock):
    tracker.add_post(
        THU, "Ещё один пост на четверг", alpha
    )  # на среду набирается два поста на дизайн
    text = build_summary(tracker, tracker.now())[0].text
    assert "дизайнеру 2 поста" in text and "ср 30.09" in text


def test_summary_reports_new_client_comments_once(tracker, board, alpha):
    from norm_tasker.tracker.models import ExternalComment
    from norm_tasker.tracker.sync import mark_comments_notified, record_comments

    mapping = {(FRI, 1): board[FRI].id}
    comment = ExternalComment(
        "c1", "kp", "Клиент Один", "поменяйте подачу, пожалуйста", None, False, FRI, 1
    )
    record_comments(tracker, [comment], mapping, {})
    reply, ids = build_summary(tracker, tracker.now())
    assert "💬 Клиент Один (КП)" in reply.text and "поменяйте подачу" in reply.text
    assert ids == ["c1"]
    mark_comments_notified(tracker, ids)
    assert "💬" not in build_summary(tracker, tracker.now())[0].text


# --- КП ---------------------------------------------------------------------------------------


def parsed(month=date(2026, 11, 1), filled=2):
    days = [date(2026, 11, d) for d in (2, 3, 5, 6, 9, 10, 11, 12, 13)]
    slots = [make_slot(d, topic="Тема") for d in days[:filled]]
    return KpParseResult(slots=slots, sheets=[SheetInfo("Ноябрь 2026", month, False, 5, 5)])


def test_kp_summary_line_phases(tracker, clock):
    clock.set(2026, 10, 10, 9, 30)
    assert summary_line(kp_status(tracker, tracker.today(), None), tracker.today()) is None
    clock.set(2026, 10, 15, 9, 30)
    line = summary_line(kp_status(tracker, tracker.today(), None), tracker.today())
    assert line == "📅 КП на ноябрь: старт вт 20.10, ок — до пт 23.10, показ клиенту пн 26.10"
    clock.set(2026, 10, 21, 9, 30)
    line = summary_line(kp_status(tracker, tracker.today(), parsed()), tracker.today())
    assert "темы есть у 2 из 21 рабочих дней" in line and "показ клиенту пн 26.10" in line
    line = summary_line(kp_status(tracker, tracker.today(), None), tracker.today())
    assert "темы есть" not in line  # КП ещё ни разу не читали


def test_kp_sheet_not_created_yet(tracker, clock):
    clock.set(2026, 10, 21, 9, 30)
    empty = KpParseResult(sheets=[SheetInfo("Октябрь 2026", date(2026, 10, 1), False, 5, 5)])
    line = summary_line(kp_status(tracker, tracker.today(), empty), tracker.today())
    assert "лист месяца ещё не создан" in line


def test_kp_line_after_show_until_marked(tracker, lead, clock):
    clock.set(2026, 10, 28, 9, 30)
    status = kp_status(tracker, tracker.today(), None)
    assert summary_line(status, tracker.today())  # показ был, отметки нет — напоминаем
    tracker.state.mark_kp_ok(date(2026, 11, 1), tracker.now())
    tracker.state.mark_kp_shown(date(2026, 11, 1), tracker.now())
    clock.set(2026, 10, 30, 9, 30)
    assert summary_line(kp_status(tracker, tracker.today(), None), tracker.today()) is None


# --- напоминания по расписанию ------------------------------------------------------------------


def names(result):
    return [rule.id for rule, _ in result]


def replies(result):
    return {rule.id: reply for rule, reply in result}


def test_rules_have_unique_ids_and_known_times(settings):
    assert len({r.id for r in RULES}) == len(RULES)
    for rule in RULES:
        assert hasattr(settings.schedule, rule.at), rule.at


def test_summary_time_window_and_catch_up(started, board, clock):
    clock.set(2026, 9, 29, 9, 29)
    assert names(due_rules(started, started.now())) == []
    clock.set(2026, 9, 29, 9, 30)
    assert "summary" in names(due_rules(started, started.now()))
    clock.set(2026, 9, 29, 12, 29)  # бот перезапустили: до 12:30 саммари ещё догоняется
    assert "summary" in names(due_rules(started, started.now()))
    clock.set(2026, 9, 29, 12, 31)
    assert "summary" not in names(due_rules(started, started.now()))


def test_rule_fires_once_a_day(started, board, clock):
    clock.set(2026, 9, 29, 9, 30)
    (rule, reply), *_ = due_rules(started, started.now())
    assert rule.id == "summary"
    mark_done(started, rule, started.now(), reply)
    assert "summary" not in names(due_rules(started, started.now()))
    clock.set(2026, 9, 30, 9, 30)  # завтра — снова
    assert "summary" in names(due_rules(started, started.now()))


def test_no_reminders_on_days_off(started, board, clock):
    clock.set(2026, 10, 3, 9, 30)  # суббота
    assert due_rules(started, started.now()) == []
    started.settings.weekend_reminders = True
    assert "summary" in names(due_rules(started, started.now()))


def test_take_reminder_at_d_minus_4(started, board, clock):
    clock.set(2026, 9, 29, 16, 0)  # для поста на пн 05.10 это D−4
    got = replies(due_rules(started, started.now()))
    reply = got["take"]
    assert f"№{board[MON].id}" in reply.text and "@lead_user" in reply.text
    assert [b.data for row in reply.buttons for b in row] == [f"claim:{board[MON].id}"]
    started.claim(board[MON].id, started.team.identify(1, "cw_alpha", "A"))
    assert replies(due_rules(started, started.now()))["take"] is None  # взят — молчим


def test_text_reminders_at_d_minus_2(started, board, alpha, beta, clock):
    started.claim(board[THU].id, beta)  # D−2 — сегодня
    clock.set(2026, 9, 29, 12, 0)
    reply = replies(due_rules(started, started.now()))["text_noon"]
    assert "до 18:00" in reply.text and "@cw_beta" in reply.text
    assert reply.buttons[0][0].data == f"stage:{board[THU].id}:{int(Stage.TEXT_SHOWN)}"
    clock.set(2026, 9, 29, 18, 0)
    reply = replies(due_rules(started, started.now()))["text_overdue"]
    assert reply.text.startswith("🔴 Просрочка") and "@lead_user" in reply.text
    started.set_stage(board[THU].id, Stage.TEXT_SHOWN, beta)
    assert replies(due_rules(started, started.now()))["text_overdue"] is None


def test_d_minus_1_reminders(started, board, alpha, lead, clock):
    started.claim(board[WED].id, alpha)
    started.set_stage(board[WED].id, Stage.TEXT_SHOWN, alpha)  # D−1 — сегодня
    clock.set(2026, 9, 29, 11, 0)
    reply = replies(due_rules(started, started.now()))["ok_nudge"]
    assert "@cw_alpha" in reply.text and "@lead_user" in reply.text and "до 13:00" in reply.text
    clock.set(2026, 9, 29, 13, 0)
    reply = replies(due_rules(started, started.now()))["handoff_hard"]
    assert "Не передано дизайнеру" in reply.text and "@lead_user" in reply.text
    started.set_stage(board[WED].id, Stage.AT_DESIGNER, lead)
    assert replies(due_rules(started, started.now()))["handoff_hard"] is None
    clock.set(2026, 9, 29, 16, 0)
    reply = replies(due_rules(started, started.now()))["show_reminder"]
    assert "до 18:00" in reply.text and "@lead_user" in reply.text
    clock.set(2026, 9, 29, 18, 0)
    reply = replies(due_rules(started, started.now()))["show_overdue"]
    assert "Просрочка" in reply.text and "@" not in reply.text  # в общий чат без тегов


def test_publish_day_reminder(started, board, lead, clock):
    started.set_stage(board[WED].id, Stage.SHOWN_DESIGN, lead)
    clock.set(2026, 9, 30, 12, 0)
    reply = replies(due_rules(started, started.now()))["final"]
    assert "нет финального ока" in reply.text and "@lead_user" in reply.text
    datas = [b.data for row in reply.buttons for b in row]
    assert f"stage:{board[WED].id}:{int(Stage.FINAL_OK)}" in datas
    assert f"stage:{board[WED].id}:{int(Stage.PUBLISHED)}" in datas


def test_soft_start_mutes_escalations(tracker, board, clock):
    assert soft_mode(tracker, tracker.now())  # запуск ещё не отмечен
    tracker.state.set_meta("first_run", "2026-09-29")
    clock.set(2026, 9, 29, 16, 0)
    result = due_rules(tracker, tracker.now())
    assert names(result) == ["take", "show_reminder"]
    assert all(reply is None for _, reply in result)  # правила отработали молча
    clock.set(2026, 9, 29, 9, 30)
    assert due_rules(tracker, tracker.now())[0][1] is not None  # саммари идёт всегда
    clock.set(2026, 10, 14, 16, 0)
    assert not soft_mode(tracker, tracker.now())  # через 14 дней мягкий старт закончился


def test_kp_reminders(started, lead, clock):
    clock.set(2026, 10, 20, 9, 30)
    reply = replies(due_rules(started, started.now()))["kp_start"]
    assert "Старт КП на ноябрь" in reply.text and reply.buttons[0][0].data == "kp:own:2026-11"
    clock.set(2026, 10, 23, 12, 0)
    reply = replies(due_rules(started, started.now()))["kp_ok_due"]
    assert "финальный ок нужен до конца дня" in reply.text and "@lead_user" in reply.text
    started.state.mark_kp_ok(date(2026, 11, 1), started.now())
    assert replies(due_rules(started, started.now()))["kp_ok_due"] is None
    clock.set(2026, 10, 26, 12, 0)
    assert (
        "Сегодня показываем клиенту КП"
        in replies(due_rules(started, started.now()))["kp_show"].text
    )
    clock.set(2026, 10, 26, 18, 0)
    assert (
        "не показано клиенту" in replies(due_rules(started, started.now()))["kp_show_overdue"].text
    )


# --- доска ---------------------------------------------------------------------------------------


def test_board(tracker, board, alpha, clock):
    tracker.claim(board[FRI].id, alpha)
    tracker.set_stage(board[FRI].id, Stage.TEXT_SHOWN, alpha)
    text = build_board(tracker, tracker.now())
    assert text.startswith("📌 Доска · обновлено 29.09 10:00")
    assert "Эта неделя (28.09–04.10)" in text and "Следующая неделя (05.10–11.10)" in text
    assert "🚀 пн 28.09" in text  # вышел из КП
    assert (
        f"📤 пт 02.10 №{board[FRI].id} «11 примеров флирта на свидании» — Копирайтер Альфа" in text
    )
    assert "⚪️ ср 30.09" in text and "свободен 🔴" in text
    assert "⚪️ свободен" in text.splitlines()[-1]  # легенда
    assert len(text) < 4096


def test_board_of_empty_tracker(tracker):
    text = build_board(tracker, tracker.now())
    assert text.count("— постов нет") == 2


# --- отчёт ---------------------------------------------------------------------------------------


def test_stage_times_from_journal(tracker, board, alpha, lead, clock):
    post = board[FRI]
    clock.set(2026, 9, 28, 10)
    tracker.claim(post.id, alpha)
    clock.set(2026, 9, 30, 15)
    tracker.set_stage(post.id, Stage.AT_DESIGNER, lead)  # прыжок через этапы
    times = stage_times(tracker, post.id)
    assert times[Stage.TAKEN].at.day == 28
    assert times[Stage.TEXT_SHOWN].at == times[Stage.TEXT_OK].at == times[Stage.AT_DESIGNER].at
    clock.set(2026, 10, 1, 9)
    tracker.set_stage(post.id, Stage.TAKEN, lead, allow_rollback=True)  # вернули на правки
    times = stage_times(tracker, post.id)
    assert set(times) == {Stage.TAKEN}


def test_weekly_report_metrics(started, board, alpha, lead, clock):
    post = board[FRI]  # D−2 = ср 30.09 18:00, D−1 = чт 01.10 18:00
    steps = [
        ((2026, 9, 28, 10), lambda: started.claim(post.id, alpha)),
        ((2026, 9, 30, 15), lambda: started.set_stage(post.id, Stage.TEXT_SHOWN, alpha)),
        ((2026, 10, 1, 9), lambda: started.set_stage(post.id, Stage.TEXT_OK, lead)),
        ((2026, 10, 1, 9, 30), lambda: started.set_stage(post.id, Stage.AT_DESIGNER, lead)),
        ((2026, 10, 1, 12), lambda: started.set_stage(post.id, Stage.DESIGN_READY, lead)),
        ((2026, 10, 1, 19), lambda: started.set_stage(post.id, Stage.SHOWN_DESIGN, lead)),
        ((2026, 10, 2, 11), lambda: started.set_stage(post.id, Stage.FINAL_OK, lead)),
        ((2026, 10, 2, 13), lambda: started.set_stage(post.id, Stage.PUBLISHED, lead)),
    ]
    for moment, action in steps:
        clock.set(*moment)
        action()
    clock.set(2026, 10, 2, 17)
    started.state.mark_reminder("text_overdue", date(2026, 9, 30), started.now(), key="sent:2")
    report = build_weekly_report(started, started.now())
    text = report.text
    assert text.startswith("📊 Итоги недели 28.09–04.10")
    assert "Вышло постов: 2 из 4" in text  # пост из КП уже был «Выпущено»
    # Пост из КП с готовым статусом в счёт не идёт (времени этапов в чате нет), а два
    # нетронутых поста с прошедшим сроком считаются несделанными.
    assert "Текст показан клиенту вовремя (D−2): 1 из 3" in text
    assert "Готовый пост показан клиенту вовремя (D−1): 0 из 3" in text
    assert "по тексту в среднем за 18 ч" in text
    assert "по готовому посту — за 16 ч" in text
    assert "дизайн занимает около 2,5 ч" in text
    assert "текст не показан клиенту вовремя — 2" in text


def test_weekly_report_without_posts(tracker):
    assert build_weekly_report(tracker, tracker.now()) is None


# --- уточнения по итогам прогона на реальном КП ---------------------------------------------


def test_topicless_posts_are_labelled_and_explained(tracker, board, clock):
    from norm_tasker import fmt

    assert fmt.label(tracker.get(board[THU].id)) == f"№{board[THU].id} (тема не выбрана)"
    assert "темы в КП ещё нет" in issue_of(tracker, board[THU]).text
    assert "темы в КП ещё нет" not in issue_of(tracker, board[FRI]).text  # тема у поста есть


def test_summary_tags_the_responsible_once(tracker, board, clock):
    clock.set(2026, 9, 29, 11, 30)
    text = build_summary(tracker, tracker.now())[0].text
    assert text.count("@lead_user") == 1
    red_block = text.split("🔴 Горит\n")[1].split("\n\n")[0]
    assert red_block.splitlines()[-1] == "@lead_user"


def test_board_labels_authors_by_stage(tracker, board, lead):
    tracker.set_stage(board[FRI].id, Stage.SHOWN_DESIGN, lead)  # этап есть, автора не отмечали
    text = build_board(tracker, tracker.now())
    line = next(x for x in text.splitlines() if f"№{board[FRI].id} " in x)
    assert "— автор не отмечен" in line and line.endswith("🟡")  # и просим отметить автора
    published = next(x for x in text.splitlines() if x.startswith("🚀"))
    assert "свободен" not in published and "автор" not in published
    assert f"№{board[THU].id} (тема не выбрана) — свободен" in text
