from datetime import date, datetime

from conftest import MSK, make_slot
from norm_tasker.config import Role
from norm_tasker.kp.models import doc_id_from_url
from norm_tasker.tracker.models import ExternalComment
from norm_tasker.tracker.stages import Stage, stage_from_kp_status
from norm_tasker.tracker.sync import (
    mark_comments_notified,
    pending_comment_notifications,
    record_comments,
    sync_kp,
)

FRI = date(2026, 10, 2)
MON = date(2026, 10, 5)
TUE = date(2026, 10, 6)
WED = date(2026, 10, 7)


def posts_by_date(tracker):
    return {(p.publish_date, p.slot): p for p in tracker._select("cancelled = 0")}


# --- сверка с КП ------------------------------------------------------------------------


def test_initial_sync_creates_posts_and_reads_kp_statuses(tracker):
    report = sync_kp(
        tracker,
        [
            make_slot(FRI, topic="Тема про пятницу и всё остальное"),
            make_slot(MON, status="В работе ", topic=None),
            make_slot(TUE, status="Текст на согласовании"),
        ],
    )
    assert report.initial
    assert len(report.created) == 3
    got = posts_by_date(tracker)
    assert got[(FRI, 1)].stage == Stage.NEW
    assert got[(MON, 1)].stage == Stage.TAKEN  # статус КП поднимает этап
    assert got[(MON, 1)].topic is None
    assert got[(TUE, 1)].stage == Stage.TEXT_SHOWN
    assert not got[(MON, 1)].assigned  # кто делает, знает только чат


def test_second_sync_changes_nothing(tracker):
    slots = [make_slot(FRI), make_slot(MON, status="В работе")]
    sync_kp(tracker, slots)
    journal_size = tracker.db.conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    report = sync_kp(tracker, slots)
    assert not report.initial
    assert not report.has_changes
    assert tracker.db.conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == journal_size
    assert len(report.slot_to_post) == 2


def test_kp_status_is_an_event_not_a_floor(tracker, alpha):
    """Откат в чате не отменяется статусом, который просто остался в таблице."""
    slots = [make_slot(FRI, status="Текст на согласовании")]
    sync_kp(tracker, slots)
    post = tracker._select()[0]
    assert post.stage == Stage.TEXT_SHOWN
    tracker.set_stage(post.id, Stage.TAKEN, alpha, allow_rollback=True)
    sync_kp(tracker, slots)  # статус в КП не менялся
    assert tracker.get(post.id).stage == Stage.TAKEN
    sync_kp(tracker, [make_slot(FRI, status="Текст готов")])  # а теперь поменялся
    assert tracker.get(post.id).stage == Stage.TEXT_OK


def test_kp_status_never_lowers_stage(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI, status="В работе")])
    post = tracker._select()[0]
    tracker.set_stage(post.id, Stage.AT_DESIGNER, alpha)
    sync_kp(tracker, [make_slot(FRI, status="Текст на согласовании")])
    assert tracker.get(post.id).stage == Stage.AT_DESIGNER


def test_post_moved_in_kp_keeps_assignee_and_recomputes_dates(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI, topic="Уникальная длинная тема для опознания", doc="docX")])
    post = tracker._select()[0]
    tracker.claim(post.id, alpha)
    report = sync_kp(
        tracker, [make_slot(TUE, topic="Уникальная длинная тема для опознания", doc="docX")]
    )
    assert [(m[0].id, m[1], m[2]) for m in report.moved] == [(post.id, FRI, TUE)]
    moved = tracker.get(post.id)
    assert moved.publish_date == TUE and moved.kp_date == TUE
    assert moved.assignee_name == alpha.name
    assert tracker.count_posts("kp") == 1  # дубля нет


def test_two_posts_swapped_in_kp_keep_their_authors(tracker, alpha, beta):
    a_topic, b_topic = "Первая достаточно длинная тема", "Вторая достаточно длинная тема"
    sync_kp(tracker, [make_slot(FRI, topic=a_topic), make_slot(MON, topic=b_topic)])
    by_topic = {p.topic: p for p in tracker._select()}
    tracker.claim(by_topic[a_topic].id, alpha)
    tracker.claim(by_topic[b_topic].id, beta)
    sync_kp(tracker, [make_slot(FRI, topic=b_topic), make_slot(MON, topic=a_topic)])
    a, b = tracker.get(by_topic[a_topic].id), tracker.get(by_topic[b_topic].id)
    assert (a.publish_date, a.assignee_name) == (MON, alpha.name)
    assert (b.publish_date, b.assignee_name) == (FRI, beta.name)


def test_topic_edit_at_same_place_is_reported(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI, topic="Старая тема про поиск пары")])
    post = tracker._select()[0]
    tracker.claim(post.id, alpha)
    report = sync_kp(tracker, [make_slot(FRI, topic="Совсем другая тема про свидания")])
    assert [(p.id, old, new) for p, old, new in report.topic_changed] == [
        (post.id, "Старая тема про поиск пары", "Совсем другая тема про свидания")
    ]
    assert tracker.get(post.id).topic == "Совсем другая тема про свидания"
    assert tracker.get(post.id).assignee_name == alpha.name


def test_moved_and_edited_topic_is_recognised_by_similarity(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI, topic="Как понять, что человек вам нравится: 5 признаков")])
    post = tracker._select()[0]
    tracker.claim(post.id, alpha)
    sync_kp(tracker, [make_slot(TUE, topic="Как понять, что человек вам нравится: 7 признаков")])
    assert tracker.count_posts("kp") == 1
    assert tracker.get(post.id).publish_date == TUE


def test_generic_topics_are_not_matched_by_text(tracker, alpha):
    """Два «мема» в разные дни не должны путаться при переносе."""
    sync_kp(tracker, [make_slot(FRI, topic="Мем"), make_slot(MON, topic="Мем")])
    fri, mon = posts_by_date(tracker)[(FRI, 1)], posts_by_date(tracker)[(MON, 1)]
    tracker.claim(fri.id, alpha)
    sync_kp(tracker, [make_slot(FRI, topic="Мем"), make_slot(MON, topic="Мем")])
    assert tracker.get(fri.id).assignee_name == alpha.name
    assert tracker.get(mon.id).assignee_name is None


def test_vanished_post_with_work_is_flagged_and_free_one_removed(tracker, alpha):
    sync_kp(
        tracker,
        [
            make_slot(FRI, topic="Пост, которым уже занимаются"),
            make_slot(MON, topic="Пост, которым никто не занимался"),
            make_slot(TUE, topic="Пост, который останется в плане"),
        ],
    )
    got = posts_by_date(tracker)
    tracker.claim(got[(FRI, 1)].id, alpha)
    report = sync_kp(tracker, [make_slot(TUE, topic="Пост, который останется в плане")])
    assert [p.id for p in report.vanished] == [got[(FRI, 1)].id]
    assert [p.id for p in report.removed] == [got[(MON, 1)].id]
    assert not tracker.get(got[(FRI, 1)].id).in_kp
    assert tracker.get(got[(FRI, 1)].id).assignee_name == alpha.name  # работа не потеряна
    assert tracker.get(got[(MON, 1)].id).cancelled
    # Повторная сверка не поднимает тревогу второй раз.
    assert not sync_kp(tracker, [make_slot(TUE, topic="Пост, который останется в плане")]).vanished


def test_mass_disappearance_is_treated_as_broken_template(tracker):
    topics = [f"Тема номер {n} для массового пропадания" for n in range(6)]
    days = [date(2026, 10, d) for d in (1, 2, 5, 6, 7, 8)]
    sync_kp(tracker, [make_slot(day, topic=t) for day, t in zip(days, topics, strict=True)])
    report = sync_kp(tracker, [make_slot(days[0], topic=topics[0])])
    assert report.suspicious == 5
    assert not report.removed and not report.vanished
    assert tracker.count_posts("kp") == 6 and not any(p.cancelled for p in tracker._select())
    # Пустая выгрузка тоже не повод всё удалить.
    assert sync_kp(tracker, []).suspicious == 6


def test_chat_move_survives_until_kp_changes(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI, topic="Тема, которую перенесут в чате")])
    post = tracker._select()[0]
    tracker.move(post.id, WED, alpha)
    sync_kp(tracker, [make_slot(FRI, topic="Тема, которую перенесут в чате")])  # КП не менялось
    assert tracker.get(post.id).publish_date == WED
    assert tracker.get(post.id).kp_date == FRI
    report = sync_kp(tracker, [make_slot(TUE, topic="Тема, которую перенесут в чате")])
    assert tracker.get(post.id).publish_date == TUE  # КП изменилось позже — оно и побеждает
    assert [(m[1], m[2]) for m in report.moved] == [(WED, TUE)]


def test_dismissed_posts_are_not_recreated(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI, topic="Заметка, принятая за пост")])
    post = tracker._select()[0]
    tracker.cancel(post.id, alpha)
    report = sync_kp(tracker, [make_slot(FRI, topic="Заметка, принятая за пост")])
    assert not report.created
    assert tracker.get(post.id).cancelled


def test_removed_without_remember_can_come_back(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI, topic="Пост, который вернут в план")])
    post = tracker._select()[0]
    tracker.cancel(post.id, alpha, remember=False)
    report = sync_kp(tracker, [make_slot(FRI, topic="Пост, который вернут в план")])
    assert len(report.created) == 1


def test_history_and_horizon_windows(tracker):
    far_past, recent, far_future = date(2026, 8, 1), date(2026, 9, 28), date(2027, 3, 1)
    sync_kp(tracker, [make_slot(far_past), make_slot(recent), make_slot(far_future)])
    assert [p.publish_date for p in tracker._select()] == [recent]


def test_chat_created_post_is_adopted_when_it_appears_in_kp(tracker, alpha):
    post = tracker.add_post(WED, "Срочный пост про акцию выходного дня", alpha)
    assert post.source == "chat" and not post.in_kp
    report = sync_kp(tracker, [make_slot(WED, topic="Срочный пост про акцию выходного дня")])
    assert not report.created
    adopted = tracker.get(post.id)
    assert adopted.source == "kp" and adopted.in_kp and adopted.kp_date == WED


def test_added_posts_get_next_free_slot(tracker, alpha):
    sync_kp(tracker, [make_slot(WED, topic="Основной пост среды")])
    extra = tracker.add_post(WED, "Дополнительный пост", alpha)
    assert extra.slot == 2


# --- действия из чата ---------------------------------------------------------------------


def test_claim_marks_author_and_stage(tracker, alpha, beta):
    sync_kp(tracker, [make_slot(FRI)])
    post = tracker._select()[0]
    result = tracker.claim(post.id, alpha, msg_link="https://t.me/c/1/2")
    assert result.ok and result.code == "ok"
    claimed = tracker.get(post.id)
    assert (claimed.assignee_id, claimed.assignee_username) == (101, "cw_alpha")
    assert claimed.stage == Stage.TAKEN
    assert claimed.last_event == f"{alpha.name}: взял пост"
    assert tracker.claim(post.id, alpha).code == "unchanged"
    other = tracker.claim(post.id, beta)
    assert (other.ok, other.code, other.previous_assignee) == (False, "taken_by_other", alpha.name)
    assert tracker.claim(post.id, beta, steal=True).ok
    assert tracker.get(post.id).assignee_username == "cw_beta"


def test_claim_refuses_published_and_missing_posts(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI, status="Выпущено")])
    assert tracker.claim(tracker._select()[0].id, alpha).code == "published"
    assert tracker.claim(999, alpha).code == "not_found"


def test_release_rules(tracker, alpha, beta, lead):
    sync_kp(tracker, [make_slot(FRI)])
    post = tracker._select()[0]
    assert tracker.release(post.id, alpha).code == "unchanged"  # некому отдавать
    tracker.claim(post.id, alpha)
    assert tracker.release(post.id, beta).code == "forbidden"
    result = tracker.release(post.id, alpha)
    assert result.ok
    released = tracker.get(post.id)
    assert not released.assigned and released.stage == Stage.NEW
    tracker.claim(post.id, alpha)
    assert tracker.release(post.id, lead).ok  # ответственный может снять с любого


def test_release_after_text_shown_keeps_stage(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI)])
    post = tracker._select()[0]
    tracker.claim(post.id, alpha)
    tracker.set_stage(post.id, Stage.TEXT_SHOWN, alpha)
    tracker.release(post.id, alpha)
    assert tracker.get(post.id).stage == Stage.TEXT_SHOWN


def test_stage_moves_forward_and_rollback_is_explicit(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI)])
    post = tracker._select()[0]
    assert tracker.set_stage(post.id, Stage.TEXT_SHOWN, alpha).ok
    assert tracker.set_stage(post.id, Stage.TEXT_SHOWN, alpha).code == "unchanged"
    assert tracker.set_stage(post.id, Stage.TAKEN, alpha).code == "later_stage"
    assert tracker.get(post.id).stage == Stage.TEXT_SHOWN
    assert tracker.set_stage(post.id, Stage.TAKEN, alpha, allow_rollback=True).ok
    assert tracker.get(post.id).stage == Stage.TAKEN


def test_copywriter_reporting_text_becomes_author_of_free_post(tracker, alpha, lead):
    sync_kp(tracker, [make_slot(FRI), make_slot(MON)])
    fri, mon = posts_by_date(tracker)[(FRI, 1)], posts_by_date(tracker)[(MON, 1)]
    tracker.set_stage(fri.id, Stage.TEXT_SHOWN, alpha)
    assert tracker.get(fri.id).assignee_name == alpha.name
    tracker.set_stage(mon.id, Stage.AT_DESIGNER, lead)  # ответственный автором не становится
    assert not tracker.get(mon.id).assigned


def test_published_by_a_chat_message_even_if_kp_is_silent(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI)])
    post = tracker._select()[0]
    tracker.set_stage(post.id, Stage.PUBLISHED, alpha)
    sync_kp(tracker, [make_slot(FRI)])  # в КП статуса всё ещё нет
    assert tracker.get(post.id).stage == Stage.PUBLISHED


# --- журнал и отмена -----------------------------------------------------------------------


def test_undo_restores_previous_state(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI)])
    post = tracker._select()[0]
    entry = tracker.claim(post.id, alpha).entry
    assert entry is not None and entry.undoable
    result = tracker.undo(entry.id, alpha)
    assert result.ok
    restored = tracker.get(post.id)
    assert not restored.assigned and restored.stage == Stage.NEW
    assert tracker.undo(entry.id, alpha).code == "already_undone"


def test_undo_rules(tracker, alpha, beta, lead):
    sync_kp(tracker, [make_slot(FRI)])
    post = tracker._select()[0]
    claim = tracker.claim(post.id, alpha).entry
    assert tracker.undo(claim.id, beta).code == "forbidden"
    stage = tracker.set_stage(post.id, Stage.TEXT_SHOWN, alpha).entry
    # Сначала отменить нужно последнее действие.
    assert tracker.undo(claim.id, alpha).code == "changed_since"
    assert tracker.undo(stage.id, lead).ok  # ответственный может отменить чужое
    assert tracker.undo(claim.id, alpha).ok
    assert tracker.get(post.id).stage == Stage.NEW


def test_undo_of_created_post_cancels_it(tracker, alpha):
    post = tracker.add_post(WED, "Срочный пост", alpha)
    entry = tracker.last_undoable(alpha)
    assert entry is not None and entry.post_id == post.id
    assert tracker.undo(entry.id, alpha).ok
    assert tracker.get(post.id).cancelled


def test_kp_changes_are_journaled_but_not_undoable(tracker, alpha):
    sync_kp(tracker, [make_slot(FRI, topic="Тема, которая потом сменится")])
    post = tracker._select()[0]
    sync_kp(tracker, [make_slot(FRI, topic="Другая тема после правки в КП")])
    kinds = [(e.kind, e.undoable) for e in tracker.history(post.id)]
    assert ("kp_sync", False) in kinds and ("kp_create", False) in kinds
    entry = next(e for e in tracker.history(post.id) if e.kind == "kp_sync")
    assert tracker.undo(entry.id, alpha).code == "not_undoable"


def test_last_undoable_is_per_actor_and_recent(tracker, alpha, beta, clock):
    sync_kp(tracker, [make_slot(FRI), make_slot(MON)])
    fri, mon = posts_by_date(tracker)[(FRI, 1)], posts_by_date(tracker)[(MON, 1)]
    tracker.claim(fri.id, alpha)
    tracker.claim(mon.id, beta)
    assert tracker.last_undoable(alpha).post_id == fri.id
    clock.set(2026, 10, 1, 12)  # прошло больше суток
    assert tracker.last_undoable(alpha) is None


# --- статусы КП и комментарии ------------------------------------------------------------------


def test_status_mapping_and_doc_id():
    assert stage_from_kp_status("В работе ") == Stage.TAKEN
    assert stage_from_kp_status("ТЕКСТ НА СОГЛАСОВАНИИ") == Stage.TEXT_SHOWN
    assert stage_from_kp_status("Выпущено") == Stage.PUBLISHED
    assert stage_from_kp_status("") is None and stage_from_kp_status("что-то новое") is None
    assert doc_id_from_url("https://docs.google.com/document/d/abc_DEF-123/edit?usp=sharing") == (
        "abc_DEF-123"
    )
    assert doc_id_from_url("https://example.com/") is None


def comment(cid, author="Клиент Один", *, day=FRI, resolved=False, text="правка"):
    return ExternalComment(
        id=cid,
        source="kp",
        author=author,
        text=text,
        created=datetime(2026, 9, 28, 12, 0, tzinfo=MSK),
        resolved=resolved,
        day=day,
        slot=1,
    )


def test_client_comments_are_reported_once(tracker):
    report = sync_kp(tracker, [make_slot(FRI)])
    mapping = report.slot_to_post
    post_id = mapping[(FRI, 1)]
    assert record_comments(tracker, [comment("old")], mapping, {}, baseline=True) == 1
    assert pending_comment_notifications(tracker) == []  # первая загрузка — без уведомлений
    record_comments(
        tracker,
        [
            comment("old"),
            comment("new"),
            comment("team", "Копирайтер Альфа"),
            comment("done", resolved=True),
            comment("elsewhere", day=date(2026, 12, 1)),  # день, которого нет в трекере
        ],
        mapping,
        {},
    )
    pending = pending_comment_notifications(tracker)
    assert [(n.comment.id, n.post.id, n.is_client) for n in pending] == [("new", post_id, True)]
    mark_comments_notified(tracker, ["new"])
    assert pending_comment_notifications(tracker) == []


def test_comment_resolved_later_is_not_reported(tracker):
    mapping = sync_kp(tracker, [make_slot(FRI)]).slot_to_post
    record_comments(tracker, [comment("c1")], mapping, {})
    record_comments(tracker, [comment("c1", resolved=True)], mapping, {})
    assert pending_comment_notifications(tracker) == []


def test_doc_comments_are_matched_by_doc_id(tracker):
    sync_kp(tracker, [make_slot(FRI, doc="docQ")])
    post = tracker._select()[0]
    doc_comment = ExternalComment(
        id="d1", source="doc", author="Клиент Два", text="тут поправьте",
        created=None, resolved=False, doc_id="docQ",
    )  # fmt: skip
    record_comments(tracker, [doc_comment], {}, {"docQ": post.id})
    assert [n.post.id for n in pending_comment_notifications(tracker)] == [post.id]


# --- команда ----------------------------------------------------------------------------------


def test_team_identification(tracker):
    team = tracker.team
    alpha = team.identify(1, "CW_Alpha", "Как-то иначе")
    assert (alpha.name, alpha.role, alpha.username) == (
        "Копирайтер Альфа",
        Role.COPYWRITER,
        "cw_alpha",
    )
    stranger = team.identify(9, "someone", "Гость")
    assert (stranger.name, stranger.role) == ("Гость", Role.COPYWRITER)  # по умолчанию свой
    tracker.settings.unknown_role = None
    assert team.identify(9, "someone", "Гость") is None


def test_mentions_and_name_search(tracker, lead):
    assert tracker.team.mention(lead) == "@lead_user"
    no_username = tracker.team.identify(55, None, "Без <ника>")
    assert tracker.team.mention(no_username) == '<a href="tg://user?id=55">Без &lt;ника&gt;</a>'
    assert tracker.team.find("Копирайтер") is None  # неоднозначно: двое
    assert tracker.team.find("@cw_beta").name == "Копирайтер Бета"
    assert tracker.team.find("Ответственный").username == "lead_user"
    assert tracker.team.responsible().username == "lead_user"
