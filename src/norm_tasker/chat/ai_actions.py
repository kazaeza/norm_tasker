"""Исполнение того, что Gemini понял из сообщения: проверка, применение, ответ.

Всё идёт через те же функции, что и обычные отчёты, поэтому работают журнал, отмена и сроки.
Здесь только лишние проверки для сообщений, которые боту не адресовали: там он осторожнее.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from norm_tasker.ai.interpreter import LISTEN_TYPES, MAX_ACTIONS, Action
from norm_tasker.chat.actions import added_post_reply, apply_one, build_reply
from norm_tasker.chat.resolver import PLAUSIBLE_BEFORE
from norm_tasker.chat.rules import IntentKind
from norm_tasker.config import Role
from norm_tasker.reply import Reply
from norm_tasker.tracker.models import Actor, Post
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import Stage

log = logging.getLogger(__name__)

KINDS = {
    "claim": IntentKind.CLAIM,
    "release": IntentKind.RELEASE,
    "stage": IntentKind.STAGE,
    "rollback": IntentKind.ROLLBACK,
    "move": IntentKind.MOVE,
    "not_post": IntentKind.NOT_POST,
}
DAYS_BACK = 30  # дальше в прошлое и вперёд дату поста не двигаем: скорее всего, ошибка
DAYS_AHEAD = 400


def combine(replies: list[Reply | None]) -> Reply | None:
    """Несколько ответов в один: тексты подряд, кнопки друг под другом."""
    items = [reply for reply in replies if reply is not None and not reply.is_empty()]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return Reply(
        "\n".join(reply.text for reply in items if reply.text),
        [row for reply in items for row in reply.buttons],
        post_ids=[post_id for reply in items for post_id in reply.post_ids],
        kind="confirm",
    )


def _fits_unaddressed(post: Post, action: Action, actor: Actor) -> bool:
    """Боту не писали: действуем, только если сказанное очень похоже на правду."""
    if post.stage >= Stage.PUBLISHED:
        return False
    if action.type == "claim":
        return not post.assigned or post.owned_by(actor)
    if action.type == "release":
        return post.owned_by(actor)
    stage = action.stage
    if stage is None:
        return False
    if stage != Stage.PUBLISHED and post.stage not in PLAUSIBLE_BEFORE.get(stage, set()):
        return False  # перескочить через этапы по обрывку разговора нельзя
    # Копирайтер отчитывается о своих постах: с чужим он мог перепутать.
    return not (
        actor.role == Role.COPYWRITER
        and stage <= Stage.TEXT_SHOWN
        and post.assigned
        and not post.owned_by(actor)
    )


def _run_one(
    tracker: Tracker,
    actor: Actor,
    action: Action,
    *,
    addressed: bool,
    msg_link: str | None,
    text: str | None,
) -> Reply | None:
    if action.day is not None and not _plausible_day(tracker.today(), action.day):
        return None
    if action.type == "add_post":
        if action.day is None:
            return None
        post = tracker.add_post(action.day, action.topic, actor, msg_link=msg_link)
        return added_post_reply(tracker, actor, post)
    kind = KINDS[action.type]
    posts = [p for p in tracker.by_ids(list(action.posts)) if not p.cancelled]
    if not addressed:
        posts = [p for p in posts if _fits_unaddressed(p, action, actor)]
    if not posts:
        return None
    applied = [
        apply_one(
            tracker,
            kind,
            post,
            actor,
            stage=action.stage,
            new_date=action.day,
            msg_link=msg_link,
            text=text,
        )
        for post in posts
    ]
    if not addressed and not any(item.result.code == "ok" for item in applied):
        return None  # ничего не изменилось: в общем чате повторный отчёт комментировать незачем
    if kind in (IntentKind.CLAIM, IntentKind.STAGE, IntentKind.ROLLBACK):
        return build_reply(tracker, kind, applied, False, actor)
    # Отдать пост, перенести, убрать: ответ собирается по одному посту.
    return combine([build_reply(tracker, kind, [item], False, actor) for item in applied])


def _plausible_day(today: date, day: date) -> bool:
    return today - timedelta(days=DAYS_BACK) <= day <= today + timedelta(days=DAYS_AHEAD)


def run_actions(
    tracker: Tracker,
    actor: Actor,
    actions: tuple[Action, ...],
    *,
    addressed: bool,
    msg_link: str | None = None,
    text: str | None = None,
) -> Reply | None:
    """Применяет действия от имени автора сообщения. None — делать было нечего или нельзя."""
    if actor.role == Role.BOSS:
        return None
    replies = []
    for action in actions[:MAX_ACTIONS]:
        if not addressed and action.type not in LISTEN_TYPES:
            log.info("Действие %s из сообщения без обращения к боту не выполняю", action.type)
            continue
        replies.append(
            _run_one(tracker, actor, action, addressed=addressed, msg_link=msg_link, text=text)
        )
    return combine(replies)
