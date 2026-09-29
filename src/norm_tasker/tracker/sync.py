"""Сверка трекера с КП.

Даты, темы и ссылки на доки берутся из КП, ответственные и этапы — из чата. Статус КП
считается событием: он поднимает этап, только когда изменился с прошлой сверки, поэтому
явный откат в чате («клиент вернул текст») не отменяется статусом, который просто остался
в таблице.

Пост в КП опознаётся по доку, по теме или по месту в календаре — в таком порядке, чтобы
перенос или обмен местами двух постов не оставил ответственного на прежней дате.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from norm_tasker.kp.models import KpSlot
from norm_tasker.tracker.models import ExternalComment, NewComment, Post, SyncReport
from norm_tasker.tracker.service import Tracker, dismiss_key, topic_key
from norm_tasker.tracker.stages import Stage, stage_from_kp_status

MIN_TOPIC_KEY = 12  # короткие темы вроде «мем» повторяются, по ним посты не опознаём
SIMILAR = 0.8  # насколько похожи темы у перенесённого и отредактированного поста
MEANINGFUL = {"publish_date", "topic", "stage", "in_kp", "source"}


def _match(
    posts: list[Post], slots: list[KpSlot]
) -> tuple[list[tuple[Post, KpSlot]], list[Post], list[KpSlot]]:
    pairs: list[tuple[Post, KpSlot]] = []
    free_posts = list(posts)
    free_slots = list(slots)

    def bind(post: Post, slot: KpSlot) -> None:
        pairs.append((post, slot))
        free_posts.remove(post)
        free_slots.remove(slot)

    # 1. По доку: ссылка не меняется при переносе и правке темы.
    for doc_id in sorted({p.doc_id for p in free_posts if p.doc_id}):
        candidates = [p for p in free_posts if p.doc_id == doc_id]
        targets = [s for s in free_slots if s.doc_id == doc_id]
        if len(candidates) == 1 and len(targets) == 1:
            bind(candidates[0], targets[0])

    # 2. По теме — достаточно длинной и одной на всю сторону.
    for key in sorted({topic_key(p.topic) for p in free_posts}):
        if len(key) < MIN_TOPIC_KEY:
            continue
        candidates = [p for p in free_posts if topic_key(p.topic) == key]
        targets = [s for s in free_slots if topic_key(s.topic) == key]
        if len(candidates) == 1 and len(targets) == 1:
            bind(candidates[0], targets[0])

    # 3. По месту в календаре: пост остался там же, мог измениться только текст темы.
    for slot in list(free_slots):
        same_place = [p for p in free_posts if p.kp_date == slot.date and p.slot == slot.slot]
        if same_place:
            bind(same_place[0], slot)

    # 4. Перенос вместе с правкой темы: ищем похожие темы среди оставшихся.
    scored = []
    for post in free_posts:
        for slot in free_slots:
            a, b = topic_key(post.topic), topic_key(slot.topic)
            if a and b:
                ratio = SequenceMatcher(None, a, b).ratio()
                if ratio >= SIMILAR:
                    scored.append((ratio, post, slot))
    for _, post, slot in sorted(scored, key=lambda item: -item[0]):
        if post in free_posts and slot in free_slots:
            bind(post, slot)

    return pairs, free_posts, free_slots


def _update_pair(
    tracker: Tracker,
    post: Post,
    slot: KpSlot,
    report: SyncReport,
    status_stages: dict[str, Stage] | None,
) -> None:
    now = tracker.now()
    changes: dict[str, object] = {}

    if post.kp_date != slot.date:
        # КП изменилось с прошлой сверки: это более свежее решение, чем перенос в чате.
        if post.publish_date != slot.date:
            report.moved.append((post, post.publish_date, slot.date))
        changes["publish_date"] = slot.date
        changes["kp_date"] = slot.date
    # Иначе КП не менялось: если пост переносили в чате, оставляем дату из чата.

    changes["slot"] = slot.slot
    changes["rubric"] = slot.rubric if slot.rubric is not None else post.rubric
    changes["topic"] = slot.topic
    changes["doc_url"] = slot.doc_url
    changes["kp_status"] = slot.status
    if post.source != "kp":
        changes["source"] = "kp"  # пост, добавленный из чата, появился и в КП
    if not post.in_kp:
        changes["in_kp"] = True

    kp_stage = stage_from_kp_status(slot.status, status_stages)
    kp_stage_int = int(kp_stage) if kp_stage is not None else None
    if kp_stage_int != post.kp_stage_seen:
        changes["kp_stage_seen"] = kp_stage_int
        if kp_stage is not None and kp_stage > post.stage:
            changes["stage"] = kp_stage
            changes["stage_at"] = now
            report.stage_from_kp.append((post, post.stage, kp_stage))

    if slot.topic != post.topic:
        report.topic_changed.append((post, post.topic, slot.topic))

    meaningful = {k: v for k, v in changes.items() if k in MEANINGFUL | {"kp_date", "stage_at"}}
    trivial = {k: v for k, v in changes.items() if k not in meaningful}
    if trivial:
        tracker._apply(
            post.id, trivial, kind="kp_sync", actor=None, source="kp", undoable=False, journal=False
        )
    if meaningful:
        tracker._apply(post.id, meaningful, kind="kp_sync", actor=None, source="kp", undoable=False)


def sync_kp(
    tracker: Tracker,
    slots: list[KpSlot],
    *,
    status_stages: dict[str, Stage] | None = None,
) -> SyncReport:
    settings = tracker.settings
    today = tracker.today()
    lo = today - timedelta(days=settings.history_days)
    hi = today + timedelta(days=settings.horizon_days)
    report = SyncReport(initial=tracker.count_posts("kp") == 0)

    matchable = [
        s
        for s in slots
        if s.date >= lo and not tracker.state.is_dismissed(dismiss_key(s.date, s.slot, s.topic))
    ]
    candidates = tracker._select(
        "cancelled = 0 AND COALESCE(kp_date, publish_date) BETWEEN ? AND ?",
        (lo.isoformat(), hi.isoformat()),
    )
    pairs, unmatched_posts, unmatched_slots = _match(candidates, matchable)

    with tracker.db.transaction():
        for post, slot in pairs:
            _update_pair(tracker, post, slot, report, status_stages)
            report.slot_to_post[slot.key] = post.id

        for slot in unmatched_slots:
            if slot.date > hi:
                continue
            kp_stage = stage_from_kp_status(slot.status, status_stages)
            created = tracker.insert_post(
                publish_date=slot.date,
                kp_date=slot.date,
                slot=slot.slot,
                rubric=slot.rubric,
                topic=slot.topic,
                doc_url=slot.doc_url,
                kp_status=slot.status,
                kp_stage_seen=int(kp_stage) if kp_stage is not None else None,
                source="kp",
                in_kp=True,
                stage=kp_stage or Stage.NEW,
                kind="kp_create",
                journal_source="kp",
                undoable=False,
            )
            report.created.append(created)
            report.slot_to_post[slot.key] = created.id

        _handle_vanished(tracker, unmatched_posts, candidates, matchable, report)

    return report


def _handle_vanished(
    tracker: Tracker,
    unmatched: list[Post],
    candidates: list[Post],
    slots: list[KpSlot],
    report: SyncReport,
) -> None:
    gone = [p for p in unmatched if p.source == "kp" and p.stage < Stage.PUBLISHED]
    if not gone:
        return
    kp_candidates = [p for p in candidates if p.source == "kp"]
    # Сломанный шаблон или пустая выгрузка выглядят как «все посты пропали»: не верим этому.
    if not slots or (len(gone) >= 4 and len(gone) > len(kp_candidates) / 2):
        report.suspicious = len(gone)
        return
    for post in gone:
        if not post.assigned and post.stage <= Stage.NEW:
            tracker.cancel(
                post.id, None, source="kp", remember=False, kind="kp_removed", undoable=False
            )
            report.removed.append(post)
        elif post.in_kp:
            tracker._apply(
                post.id, {"in_kp": False}, kind="kp_gone", actor=None, source="kp", undoable=False
            )
            report.vanished.append(post)


# --- комментарии --------------------------------------------------------------------------


def is_client_author(tracker: Tracker, author: str) -> bool:
    """Автор, которого нет в списке команды, считается клиентом."""
    team = {name.casefold() for name in tracker.settings.team_authors}
    return author.casefold() not in team


def record_comments(
    tracker: Tracker,
    comments: list[ExternalComment],
    slot_to_post: dict[tuple[date, int], int],
    doc_to_post: dict[str, int],
    *,
    baseline: bool = False,
) -> int:
    """Запоминает комментарии; baseline — первая загрузка, о старых комментариях не сообщаем."""
    now = tracker.now().isoformat()
    new = 0
    with tracker.db.transaction():
        for comment in comments:
            post_id = None
            if comment.source == "kp" and comment.day is not None and comment.slot is not None:
                post_id = slot_to_post.get((comment.day, comment.slot))
            elif comment.source == "doc" and comment.doc_id:
                post_id = doc_to_post.get(comment.doc_id)
            if post_id is None:
                continue
            client = is_client_author(tracker, comment.author)
            row = tracker.db.conn.execute(
                "SELECT resolved, post_id FROM comments WHERE id = ?", (comment.id,)
            ).fetchone()
            if row is None:
                silent = baseline or comment.resolved or not client
                tracker.db.conn.execute(
                    "INSERT INTO comments (id, source, post_id, author, is_client, text, "
                    "created_at, "
                    "resolved, notified, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        comment.id,
                        comment.source,
                        post_id,
                        comment.author,
                        int(client),
                        comment.text,
                        comment.created.isoformat() if comment.created else None,
                        int(comment.resolved),
                        int(silent),
                        now,
                    ),
                )
                new += 1
            elif bool(row["resolved"]) != comment.resolved or row["post_id"] != post_id:
                tracker.db.conn.execute(
                    "UPDATE comments SET resolved = ?, post_id = ? WHERE id = ?",
                    (int(comment.resolved), post_id, comment.id),
                )
    return new


def pending_comment_notifications(tracker: Tracker) -> list[NewComment]:
    """Новые непрочитанные комментарии клиента к постам, которые ещё не вышли."""
    rows = tracker.db.conn.execute(
        "SELECT c.id AS cid, c.source, c.author, c.text, c.created_at, c.resolved, c.post_id "
        "FROM comments c JOIN posts p ON p.id = c.post_id "
        "WHERE c.notified = 0 AND c.is_client = 1 AND c.resolved = 0 "
        "AND p.cancelled = 0 AND p.stage < ? ORDER BY c.created_at, c.id",
        (int(Stage.PUBLISHED),),
    ).fetchall()
    result = []
    for row in rows:
        post = tracker.get(row["post_id"])
        if post is None:
            continue
        created = datetime.fromisoformat(row["created_at"]) if row["created_at"] else None
        comment = ExternalComment(
            id=row["cid"],
            source=row["source"],
            author=row["author"],
            text=row["text"] or "",
            created=created,
            resolved=False,
        )
        result.append(NewComment(post, comment, True))
    return result


def mark_comments_notified(tracker: Tracker, ids: list[str]) -> None:
    for comment_id in ids:
        tracker.db.conn.execute("UPDATE comments SET notified = 1 WHERE id = ?", (comment_id,))


def comments_of(tracker: Tracker, post_id: int, only_open: bool = True) -> list[ExternalComment]:
    rows = tracker.db.conn.execute(
        "SELECT * FROM comments WHERE post_id = ? AND is_client = 1"
        + (" AND resolved = 0" if only_open else "")
        + " ORDER BY created_at, id",
        (post_id,),
    ).fetchall()
    return [
        ExternalComment(
            id=r["id"],
            source=r["source"],
            author=r["author"],
            text=r["text"] or "",
            created=datetime.fromisoformat(r["created_at"]) if r["created_at"] else None,
            resolved=bool(r["resolved"]),
        )
        for r in rows
    ]
