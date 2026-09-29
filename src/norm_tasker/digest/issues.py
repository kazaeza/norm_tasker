"""Что с постом не так прямо сейчас.

Для каждого поста ищется единственная самая важная проблема: бот пишет одну строку на пост,
а не поток сообщений. Уровни: red — срок сорван или сорвётся, если не вмешаться;
yellow — срок сегодня.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from norm_tasker import fmt
from norm_tasker.config import Role
from norm_tasker.tracker.models import Post
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import LABEL, Stage

Level = Literal["red", "yellow"]


@dataclass(frozen=True)
class Issue:
    post: Post
    code: str
    level: Level
    text: str  # без названия поста: «ока по тексту нет…»
    role: Role | None = None  # кому это решать: автору или ответственному за проект


def post_issue(tracker: Tracker, post: Post, now: datetime) -> Issue | None:
    if post.cancelled or post.stage >= Stage.PUBLISHED:
        return None
    chain = tracker.chain(post)
    today = now.date()
    stage = post.stage
    owner = post.assigned

    def issue(code: str, level: Level, text: str, role: Role | None = None) -> Issue:
        return Issue(post, code, level, text, role)

    # Дата выхода прошла, а «вышел» не отмечено.
    if today > post.publish_date:
        return issue(
            "unmarked",
            "yellow",
            f"выходил {fmt.wd_date(post.publish_date)}: отметьте «вышел», если пост опубликован",
            Role.RESPONSIBLE,
        )

    # Сегодня выход.
    if today == post.publish_date:
        if stage < Stage.SHOWN_DESIGN:
            return issue(
                "late_today",
                "red",
                f"выходит сегодня, а этап — «{LABEL[stage]}»: клиенту готовый пост не показан",
                Role.RESPONSIBLE,
            )
        if stage == Stage.SHOWN_DESIGN:
            level: Level = "red" if now > chain.final_check else "yellow"
            return issue("final_wait", level, "ждём финальный ок клиента", Role.RESPONSIBLE)
        return issue(
            "publish",
            "yellow",
            "финальный ок есть — опубликуйте или поставьте в отложенные",
            Role.RESPONSIBLE,
        )

    # День показа клиенту (D−1) и дальше до выхода.
    if today >= chain.show_day:
        on_day = today == chain.show_day
        deadline = (
            f"сегодня до {chain.show_by:%H:%M}" if on_day else f"до {fmt.dt_short(chain.show_by)}"
        )
        overdue = now > chain.show_by
        if stage <= Stage.TAKEN:
            who = "никто не взял" if not owner else "текст не показан клиенту"
            tail = (
                f"а показать пост с дизайном нужно {deadline}"
                if not overdue
                else f"срок показа с дизайном был {fmt.dt_short(chain.show_by)}"
            )
            return issue("show_blocked", "red", f"{who}, {tail}", Role.RESPONSIBLE)
        if stage == Stage.TEXT_SHOWN:
            hard = f"{chain.handoff_hard:%H:%M}"
            if now < chain.text_ok_warn:
                return issue(
                    "ok_wait",
                    "yellow",
                    f"ока по тексту пока нет. Дизайнеру нужно передать до {hard}",
                    Role.RESPONSIBLE,
                )
            if now < chain.text_ok_hard:
                return issue(
                    "ok_wait",
                    "red",
                    "ока по тексту нет — пора напомнить клиенту. "
                    f"Дизайнеру нужно передать до {hard}, иначе пост не будет показан клиенту "
                    f"{'сегодня' if on_day else 'вовремя'}",
                    Role.RESPONSIBLE,
                )
            return issue(
                "ok_late",
                "red",
                f"ока по тексту всё ещё нет, дизайн не успеет к {chain.show_by:%H:%M}: "
                "нужно решение — показать без дизайна или договориться о переносе",
                Role.RESPONSIBLE,
            )
        if stage == Stage.TEXT_OK:
            if now <= chain.handoff_ideal:
                return issue(
                    "handoff",
                    "yellow",
                    f"текст одобрен — передать дизайнеру до {chain.handoff_ideal:%H:%M} "
                    f"(крайний срок {chain.handoff_hard:%H:%M})",
                    Role.RESPONSIBLE,
                )
            if now <= chain.handoff_hard:
                return issue(
                    "handoff",
                    "yellow",
                    "текст одобрен, дизайнеру не передан — "
                    f"крайний срок {chain.handoff_hard:%H:%M}",
                    Role.RESPONSIBLE,
                )
            return issue(
                "handoff_late",
                "red",
                f"не передан дизайнеру (крайний срок был {chain.handoff_hard:%H:%M}): "
                "в срок не успеем, нужно решение",
                Role.RESPONSIBLE,
            )
        if stage in (Stage.AT_DESIGNER, Stage.DESIGN_READY):
            if overdue:
                return issue(
                    "show_late",
                    "red",
                    f"не показан клиенту, срок был {fmt.dt_short(chain.show_by)}",
                    Role.RESPONSIBLE,
                )
            what = "у дизайнера" if stage == Stage.AT_DESIGNER else "дизайн готов"
            return issue(
                "show_due", "yellow", f"{what} — показать клиенту {deadline}", Role.RESPONSIBLE
            )
        return None  # SHOWN_DESIGN и позже: ждём ок клиента, вмешиваться рано

    # День текста (D−2) и дни между текстом и показом (выходные).
    if today >= chain.text_day:
        if stage <= Stage.NEW and not owner:
            return issue(
                "text_no_owner",
                "red",
                "никто не взял, а текст клиенту нужен "
                + (
                    f"сегодня до {chain.text_shown_by:%H:%M}"
                    if today == chain.text_day
                    else f"был до {fmt.dt_short(chain.text_shown_by)}"
                ),
                Role.RESPONSIBLE,
            )
        if stage <= Stage.TAKEN:
            if now > chain.text_shown_by:
                return issue(
                    "text_late",
                    "red",
                    f"текст не показан клиенту, срок был {fmt.dt_short(chain.text_shown_by)}",
                    Role.RESPONSIBLE,
                )
            return issue(
                "text_due",
                "yellow",
                f"текст должен быть готов и показан клиенту до {chain.text_shown_by:%H:%M}",
                Role.COPYWRITER,
            )
        return None

    # День, когда пост надо взять (D−4), и дни после него.
    if today >= chain.take_day and not owner:
        if stage >= Stage.TAKEN:  # в КП «В работе», а в чате о владельце ничего нет
            return issue(
                "no_owner",
                "yellow",
                "в КП пост «в работе», а кто его делает — не отмечено (напишите «беру»)",
                Role.COPYWRITER,
            )
        if today == chain.take_day and now <= chain.take_by:
            return issue(
                "take_due",
                "yellow",
                "никто не взял — брать нужно сегодня, "
                f"текст клиенту до {fmt.dt_short(chain.text_shown_by)}",
                Role.COPYWRITER,
            )
        return issue(
            "take_late",
            "red",
            f"никто не взял, срок был {fmt.dt_short(chain.take_by)}; "
            f"текст клиенту — до {fmt.dt_short(chain.text_shown_by)}",
            Role.RESPONSIBLE,
        )
    return None


def issues_for(tracker: Tracker, posts: list[Post], now: datetime) -> list[Issue]:
    found = [i for p in posts if (i := post_issue(tracker, p, now)) is not None]
    return sorted(found, key=lambda i: (i.post.publish_date, i.post.slot, i.post.id))
