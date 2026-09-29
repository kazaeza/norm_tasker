"""Какой пост имеется в виду.

Порядок поиска: сообщение бота, на которое ответили; номер; дата или день недели;
рубрика; слова из темы; наконец, контекст — посты автора, которым подходит такой этап.
Если уверенности нет, бот предлагает варианты кнопками.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from norm_tasker.chat.refs import PostRef, topic_score
from norm_tasker.chat.rules import Intent, IntentKind
from norm_tasker.config import Role
from norm_tasker.tracker.models import Actor, Post
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import Stage, normalize

STAGE_LIKE = (IntentKind.STAGE, IntentKind.CLIENT_OK, IntentKind.SHOWN_CLIENT)
MAX_OPTIONS = 6
BACK_DAYS = 3
AHEAD_DAYS = 45

# С каких этапов пост может перейти на указанный. Нужно, только когда пост в сообщении
# не назван: с явной ссылкой (номер, ответ бота, дата) этап не проверяем.
PLAUSIBLE_BEFORE: dict[Stage, set[Stage]] = {
    Stage.TAKEN: {Stage.NEW},
    Stage.TEXT_SHOWN: {Stage.NEW, Stage.TAKEN},
    Stage.TEXT_OK: {Stage.TEXT_SHOWN},
    Stage.AT_DESIGNER: {Stage.TEXT_OK, Stage.TEXT_SHOWN},
    Stage.DESIGN_READY: {Stage.AT_DESIGNER},
    Stage.SHOWN_DESIGN: {Stage.DESIGN_READY, Stage.AT_DESIGNER, Stage.TEXT_OK},
    Stage.FINAL_OK: {Stage.SHOWN_DESIGN, Stage.DESIGN_READY},
}


@dataclass
class Resolution:
    status: Literal["resolved", "ambiguous", "none"]
    posts: list[Post] = field(default_factory=list)
    # Уверенно ли выбран пост: номер, ответ на сообщение бота, дата — да; догадка — нет.
    confident: bool = True
    # Для «клиент ок» и «показал клиенту»: какой этап ставить каждому посту.
    stages: dict[int, Stage] = field(default_factory=dict)


def effective_stage(kind: IntentKind, post: Post, stage: Stage | None) -> Stage | None:
    """Этап, на который надо перевести пост, когда фраза не называет его прямо."""
    if kind == IntentKind.CLIENT_OK:
        return Stage.TEXT_OK if post.stage <= Stage.TEXT_SHOWN else Stage.FINAL_OK
    if kind == IntentKind.SHOWN_CLIENT:
        return Stage.TEXT_SHOWN if post.stage <= Stage.TAKEN else Stage.SHOWN_DESIGN
    return stage


def _matches_rubric(post: Post, rubrics: tuple[str, ...]) -> bool:
    return normalize(post.rubric) in {normalize(r) for r in rubrics}


def _relevant(posts: list[Post], intent: Intent, actor: Actor, explicit: bool) -> list[Post]:
    """Оставляет посты, к которым сообщение может относиться по смыслу."""
    live = [p for p in posts if not p.cancelled]
    kind = intent.kind
    if explicit and kind in STAGE_LIKE:
        return live  # пост назван прямо: не гадаем, подходит ли ему этап
    if kind in (IntentKind.CLAIM, IntentKind.ASSIGN):
        return [p for p in live if p.stage < Stage.PUBLISHED]
    if kind == IntentKind.RELEASE:
        pool = [p for p in live if p.assigned and p.stage < Stage.PUBLISHED]
        if actor.role == Role.RESPONSIBLE and explicit:
            return pool
        return [p for p in pool if p.owned_by(actor)]
    if kind == IntentKind.STAGE and intent.stage is not None:
        if intent.stage == Stage.PUBLISHED:
            return [p for p in live if p.stage < Stage.PUBLISHED]
        return [p for p in live if p.stage in PLAUSIBLE_BEFORE.get(intent.stage, set())]
    if kind == IntentKind.CLIENT_OK:
        return [p for p in live if Stage.TAKEN <= p.stage <= Stage.SHOWN_DESIGN]
    if kind == IntentKind.SHOWN_CLIENT:
        return [p for p in live if Stage.TAKEN <= p.stage <= Stage.DESIGN_READY]
    if kind == IntentKind.ROLLBACK:
        return [p for p in live if Stage.TEXT_SHOWN <= p.stage < Stage.PUBLISHED]
    if kind in (IntentKind.MOVE, IntentKind.NOT_POST):
        return [p for p in live if p.stage < Stage.PUBLISHED or explicit]
    return live


def _by_dates(posts: list[Post], ref: PostRef) -> list[Post]:
    return [
        p
        for p in posts
        if p.publish_date in ref.dates
        or p.publish_date.weekday() in ref.weekdays
        or p.publish_date.day in ref.day_numbers
    ]


def _nearest_per_weekday(posts: list[Post], ref: PostRef, today: date) -> tuple[list[Post], bool]:
    """«В пятницу» без даты — ближайшая пятница, а не все пятницы календаря.

    Второй элемент — пришлось ли выбирать из нескольких недель (тогда это догадка).
    """
    if not ref.weekdays or ref.dates or ref.day_numbers:
        return posts, False
    guessed = False
    result = [p for p in posts if p.publish_date.weekday() not in ref.weekdays]
    for weekday in ref.weekdays:
        of_day = sorted(
            (p for p in posts if p.publish_date.weekday() == weekday),
            key=lambda p: (p.publish_date < today - timedelta(days=1), p.publish_date, p.slot),
        )
        if not of_day:
            continue
        nearest = of_day[0].publish_date
        guessed = guessed or any(p.publish_date != nearest for p in of_day)
        result.extend(p for p in of_day if p.publish_date == nearest)
    return result, guessed


def _context_candidates(posts: list[Post], intent: Intent, actor: Actor, today: date) -> list[Post]:
    """Сообщение не назвало пост: подбираем по контексту."""
    kind = intent.kind
    if kind in (IntentKind.CLAIM, IntentKind.ASSIGN):
        return [p for p in posts if not p.assigned]
    if kind in (
        IntentKind.STAGE,
        IntentKind.CLIENT_OK,
        IntentKind.SHOWN_CLIENT,
        IntentKind.ROLLBACK,
    ):
        if intent.stage == Stage.PUBLISHED:
            # «Вышел» — про пост, который уже должен был выйти; ближайший к сегодня — первым.
            due = [p for p in posts if p.publish_date <= today]
            today_only = [p for p in due if p.publish_date == today]
            return today_only or due
        if actor.role == Role.COPYWRITER:
            # Копирайтер, за которым ничего не записано, мог говорить о чём угодно: молчим.
            return [p for p in posts if p.owned_by(actor)]
        return posts
    if kind == IntentKind.RELEASE:
        return posts
    return []


def resolve(
    tracker: Tracker,
    intent: Intent,
    actor: Actor,
    *,
    reply_post_ids: list[int] | None = None,
) -> Resolution:
    today = tracker.today()
    ref = intent.ref
    by_number = bool(ref.numbers)
    by_reply = bool(reply_post_ids) and not by_number
    anchored = by_number or by_reply  # явная привязка к сообщению бота или к номеру
    guessed = False

    if by_number:
        pool = _relevant(
            [p for p in tracker.by_ids(list(ref.numbers)) if not p.cancelled],
            intent,
            actor,
            explicit=True,
        )
    else:
        if by_reply:
            universe = [p for p in tracker.by_ids(reply_post_ids or []) if not p.cancelled]
        else:
            universe = tracker.posts_between(
                today - timedelta(days=BACK_DAYS), today + timedelta(days=AHEAD_DAYS)
            )
        pool = _relevant(universe, intent, actor, explicit=by_reply)
        if intent.kind in (*STAGE_LIKE, IntentKind.ROLLBACK) and actor.role == Role.COPYWRITER:
            # Копирайтер отчитывается о своих постах; чужие берём, только если своих нет.
            pool = [p for p in pool if p.owned_by(actor)] or pool
        if ref.has_date:
            pool = _by_dates(pool, ref)
            if intent.kind in (IntentKind.CLAIM, IntentKind.ASSIGN):
                pool = [p for p in pool if not p.assigned] or pool
        if ref.rubrics:
            pool = [p for p in pool if _matches_rubric(p, ref.rubrics)]
        if ref.words:
            scored = [(topic_score(ref.words, p.topic), p) for p in pool]
            best = max((s for s, _ in scored), default=0)
            if best > 0:
                pool = [p for s, p in scored if s == best]
            elif not (ref.has_date or ref.rubrics or by_reply):
                pool = []  # слова не совпали ни с одной темой и других зацепок нет
        if ref.has_date:
            pool, guessed = _nearest_per_weekday(pool, ref, today)
        if ref.is_empty and not by_reply:
            pool = _context_candidates(pool, intent, actor, today)
            guessed = True

    pool = sorted(pool, key=lambda p: (p.publish_date, p.slot, p.id))
    if not pool:
        return Resolution("none")

    confident = anchored or (ref.has_date and not guessed)
    # Несколько дат в одном сообщении («на пятницу и на понедельник») — несколько постов.
    named_days = len(ref.dates) + len(ref.weekdays) + len(ref.day_numbers)
    one_per_day = len({p.publish_date for p in pool}) == len(pool)
    multi = named_days > 1 and one_per_day and len(pool) <= named_days
    if len(pool) == 1 or multi:
        stages = {
            p.id: st
            for p in pool
            if (st := effective_stage(intent.kind, p, intent.stage)) is not None
        }
        return Resolution("resolved", pool, confident, stages)
    return Resolution("ambiguous", pool[:MAX_OPTIONS], False)
