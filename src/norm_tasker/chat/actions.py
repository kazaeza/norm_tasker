"""От сообщения к изменению трекера и ответу бота.

`handle_message` — весь путь одного сообщения команды: распознать, выбрать пост, применить,
подготовить ответ. `handle_callback` — то же для нажатий кнопок. Telegram здесь не нужен:
ответ возвращается данными (`Reply`), а отправляет его слой бота.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from typing import Any

from norm_tasker import fmt
from norm_tasker.chat.resolver import Resolution, effective_stage, resolve
from norm_tasker.chat.rules import Intent, IntentKind, parse_message
from norm_tasker.config import Role
from norm_tasker.digest.kp import kp_status, month_from_key
from norm_tasker.kp.models import KpParseResult
from norm_tasker.reply import Button, Reply, claim_button
from norm_tasker.tracker.models import Actor, Post
from norm_tasker.tracker.service import ActionResult, Tracker
from norm_tasker.tracker.stages import EMOJI, LABEL, Stage

STAGE_KINDS = (IntentKind.STAGE, IntentKind.CLIENT_OK, IntentKind.SHOWN_CLIENT)
SILENT_WHEN_MISSING = (*STAGE_KINDS, IntentKind.ROLLBACK, IntentKind.RELEASE)


def undo_button(entry_id: int, text: str = "↩️ Отменить") -> Button:
    return Button(text, f"undo:{entry_id}")


# --- применение к одному посту --------------------------------------------------------------


@dataclass
class Applied:
    post: Post | None
    result: ActionResult
    stage: Stage | None = None
    new_date: date | None = None


def _next_weekday(after: date, weekday: int) -> date:
    days = (weekday - after.weekday()) % 7 or 7
    return after + timedelta(days=days)


def apply_one(
    tracker: Tracker,
    kind: IntentKind,
    post: Post,
    actor: Actor,
    *,
    stage: Stage | None = None,
    new_date: date | None = None,
    new_weekday: int | None = None,
    msg_link: str | None = None,
    text: str | None = None,
    source: str = "chat",
    steal: bool = False,
) -> Applied:
    common: dict[str, Any] = {"source": source, "msg_link": msg_link, "text": text}
    if kind == IntentKind.CLAIM:
        return Applied(post, tracker.claim(post.id, actor, steal=steal, **common))
    if kind == IntentKind.RELEASE:
        return Applied(post, tracker.release(post.id, actor, **common))
    if kind in STAGE_KINDS or kind == IntentKind.ROLLBACK:
        if stage is None:
            return Applied(post, ActionResult(False, "not_found", post))
        result = tracker.set_stage(
            post.id, stage, actor, allow_rollback=kind == IntentKind.ROLLBACK, **common
        )
        return Applied(post, result, stage)
    if kind == IntentKind.MOVE:
        target = new_date
        if target is None and new_weekday is not None:
            target = _next_weekday(post.publish_date, new_weekday)
        if target is None:
            return Applied(post, ActionResult(False, "not_found", post))
        return Applied(post, tracker.move(post.id, target, actor, **common), None, target)
    if kind == IntentKind.NOT_POST:
        return Applied(post, tracker.cancel(post.id, actor, **common))
    return Applied(post, ActionResult(False, "not_found", post))


# --- ответы ------------------------------------------------------------------------------


def _deadline_note(tracker: Tracker, post: Post, stage: Stage) -> str:
    chain = tracker.chain(post)
    if stage == Stage.TAKEN:
        return f"Текст клиенту — до {fmt.dt_short(chain.text_shown_by)}."
    if stage == Stage.TEXT_SHOWN:
        return f"Ок по тексту нужен до {fmt.dt_short(chain.text_ok_hard)}."
    if stage in (Stage.TEXT_OK, Stage.AT_DESIGNER):
        return f"Показать клиенту с дизайном — до {fmt.dt_short(chain.show_by)}."
    return ""


def _late_warning(tracker: Tracker, post: Post, stage: Stage) -> str:
    """Предупреждение, если этап наступил слишком поздно для срока дальше по цепочке."""
    chain = tracker.chain(post)
    now = tracker.now()
    if stage == Stage.TAKEN and now > chain.text_shown_by:
        return f"⏰ Срок показа текста клиенту уже прошёл ({fmt.dt_short(chain.text_shown_by)})."
    late_handoff = stage in (Stage.TEXT_OK, Stage.AT_DESIGNER) and now > chain.handoff_hard
    if late_handoff and now < chain.show_by:
        return (
            f"⚠️ Дизайнеру пост передан после {chain.handoff_hard:%H:%M}: "
            f"показать клиенту до {chain.show_by:%H:%M} может не получиться."
        )
    return ""


def _claim_reply(tracker: Tracker, applied: list[Applied], actor: Actor) -> Reply:
    lines: list[str] = []
    buttons: list[Button] = []
    ids: list[int] = []
    for item in applied:
        post = item.result.post or item.post
        if post is None:
            continue
        ids.append(post.id)
        code = item.result.code
        if code == "ok":
            lines.append(
                f"✍️ {escape(actor.name)} — {fmt.line(post)}. "
                f"{_deadline_note(tracker, post, Stage.TAKEN)}"
            )
            warning = _late_warning(tracker, post, Stage.TAKEN)
            if warning:
                lines.append(warning)
            if item.result.entry:
                buttons.append(undo_button(item.result.entry.id, f"↩️ Отменить №{post.id}"))
        elif code == "unchanged":
            lines.append(f"{fmt.label(post)} уже за вами.")
        elif code == "taken_by_other":
            lines.append(f"{fmt.line(post)} уже у {escape(item.result.previous_assignee or '?')}.")
            buttons.append(Button(f"Забрать №{post.id}", f"steal:{post.id}"))
        elif code == "published":
            lines.append(f"{fmt.label(post)} уже вышел.")
    if not lines:
        return Reply()
    return Reply("\n".join(lines), [buttons] if buttons else [], post_ids=ids, kind="confirm")


def _stage_reply(tracker: Tracker, applied: list[Applied], confident: bool) -> Reply:
    lines: list[str] = []
    buttons: list[Button] = []
    ids: list[int] = []
    extra = False
    for item in applied:
        post = item.result.post or item.post
        if post is None or item.stage is None:
            continue
        ids.append(post.id)
        code = item.result.code
        if code == "ok":
            lines.append(f"{EMOJI[item.stage]} {fmt.line(post)} — {LABEL[item.stage]}.")
            note = _deadline_note(tracker, post, item.stage)
            if note and item.stage in (Stage.TEXT_SHOWN, Stage.TEXT_OK, Stage.AT_DESIGNER):
                lines[-1] += f" {note}"
                extra = extra or item.stage == Stage.AT_DESIGNER
            warning = _late_warning(tracker, post, item.stage)
            if warning:
                lines.append(warning)
                extra = True
            if item.result.entry:
                buttons.append(undo_button(item.result.entry.id, f"↩️ Отменить №{post.id}"))
        elif code == "unchanged":
            lines.append(f"{fmt.label(post)} уже на этапе «{LABEL[item.stage]}».")
        elif code == "later_stage":
            lines.append(f"{fmt.label(post)} уже дальше: {LABEL[post.stage]}. Ничего не меняю.")
    if not lines:
        return Reply()
    changed = any(item.result.code == "ok" for item in applied)
    quiet = confident and len(applied) == 1 and not extra
    if changed and quiet and applied[0].stage != Stage.AT_DESIGNER:
        return Reply(react=True, post_ids=ids)  # уверены и добавить нечего — хватит 👍
    if applied[0].result.code != "ok" and confident and len(applied) == 1:
        return Reply(react=True, post_ids=ids)  # уже так и записано
    return Reply(
        "\n".join(lines),
        [buttons] if buttons else [],
        react=changed and confident,
        post_ids=ids,
        kind="confirm",
    )


def _simple_reply(kind: IntentKind, tracker: Tracker, item: Applied, actor: Actor) -> Reply:
    post = item.result.post or item.post
    if post is None:
        return Reply()
    code = item.result.code
    entry = item.result.entry
    undo = [[undo_button(entry.id)]] if entry else []
    if kind == IntentKind.RELEASE:
        if code == "ok":
            return Reply(
                f"🙋 {fmt.line(post)} снова свободен. Кто возьмёт?",
                [[claim_button(post.id)], *undo],
                post_ids=[post.id],
                kind="free",
            )
        if code == "forbidden":
            who = escape(item.result.previous_assignee or "другого автора")
            return Reply(f"{fmt.label(post)} закреплён за {who}, снять его может только он сам.")
        return Reply()
    if kind == IntentKind.MOVE:
        if code == "ok":
            chain = tracker.chain(post)
            return Reply(
                f"📆 {fmt.label(post)} → {fmt.wd_date(post.publish_date)}. "
                f"Текст клиенту — до {fmt.dt_short(chain.text_shown_by)}, "
                f"дизайн и показ — до {fmt.dt_short(chain.show_by)}.\n"
                "Поправьте, пожалуйста, и КП.",
                undo,
                post_ids=[post.id],
                kind="confirm",
            )
        return Reply()
    if kind == IntentKind.NOT_POST and code == "ok":
        return Reply(
            f"🗑 {fmt.label(post)} убран из трекера. Такой пост больше создавать не буду.",
            undo,
            kind="confirm",
        )
    return Reply()


def build_reply(
    tracker: Tracker, kind: IntentKind, applied: list[Applied], confident: bool, actor: Actor
) -> Reply:
    if not applied:
        return Reply()
    if kind == IntentKind.CLAIM:
        return _claim_reply(tracker, applied, actor)
    if kind in STAGE_KINDS or kind == IntentKind.ROLLBACK:
        reply = _stage_reply(tracker, applied, confident and kind != IntentKind.ROLLBACK)
        return reply
    return _simple_reply(kind, tracker, applied[0], actor)


# --- вопрос «какой пост?» --------------------------------------------------------------------


def _ask_which(tracker: Tracker, intent: Intent, res: Resolution, actor: Actor, text: str,
               msg_link: str | None) -> Reply:  # fmt: skip
    payload = {
        "kind": intent.kind.value,
        "stage": int(intent.stage) if intent.stage is not None else None,
        "new_date": intent.new_date.isoformat() if intent.new_date else None,
        "new_weekday": intent.new_weekday,
        "text": text[:200],
        "msg_link": msg_link,
    }
    pending_id = tracker.state.create_pending("pick", payload, actor.id, tracker.now())
    rows = [
        [
            Button(
                f"{fmt.wd_date(p.publish_date)} №{p.id} {fmt.clip(p.title, 28)}",
                f"pick:{pending_id}:{p.id}",
            )
        ]
        for p in res.posts
    ]
    rows.append([Button("Никакой", f"pick:{pending_id}:0")])
    return Reply("Какой пост имеется в виду?", rows, kind="ask")


def _missing_reply(intent: Intent) -> Reply:
    ref = intent.ref
    anchored = bool(ref.numbers or ref.has_date or ref.rubrics)  # одни слова — может быть болтовня
    if intent.kind in SILENT_WHEN_MISSING or not anchored:
        return Reply()
    return Reply("Не вижу такого поста в трекере. Посты на неделю — /week, свободные — /free.")


# --- КП -----------------------------------------------------------------------------------------


def _handle_kp(
    tracker: Tracker, intent: Intent, actor: Actor, parsed: KpParseResult | None
) -> Reply:
    status = kp_status(tracker, tracker.today(), parsed)
    month = status.timeline.month
    name = status.month_name
    if intent.kind == IntentKind.KP_OWN:
        tracker.state.set_kp_owner(month, actor)
        return Reply(
            f"📅 КП на {name}: сборку ведёт {escape(actor.name)}.", react=False, kind="confirm"
        )
    if intent.kind == IntentKind.KP_OK:
        if actor.role != Role.RESPONSIBLE:
            return Reply(
                "Финальный ок по КП даёт ответственный за проект — "
                f"{tracker.team.mention_responsible()}."
            )
        tracker.state.mark_kp_ok(month, tracker.now())
        return Reply(
            f"📅 КП на {name}: финальный ок записан. Показ клиенту — "
            f"{fmt.wd_date(status.timeline.show_date)}.",
            kind="confirm",
        )
    if intent.kind == IntentKind.KP_SHOWN:
        tracker.state.mark_kp_shown(month, tracker.now())
        return Reply(react=True)
    return Reply()


# --- главный вход ---------------------------------------------------------------------------------


def handle_message(
    tracker: Tracker,
    text: str,
    actor: Actor,
    *,
    reply_post_ids: list[int] | None = None,
    msg_link: str | None = None,
    parsed_kp: KpParseResult | None = None,
) -> Reply | None:
    """Ответ на сообщение команды; None — бот ничего не делает и молчит."""
    if actor.role == Role.BOSS:
        return None
    intent = parse_message(text, tracker.today(), tuple(tracker.settings.client_names))
    if intent is None:
        return None
    if intent.kind in (IntentKind.KP_OK, IntentKind.KP_SHOWN, IntentKind.KP_OWN):
        return _handle_kp(tracker, intent, actor, parsed_kp)
    if intent.kind == IntentKind.ASSIGN:
        return _handle_assign(tracker, intent, actor, reply_post_ids, text)

    res = resolve(tracker, intent, actor, reply_post_ids=reply_post_ids)
    if res.status == "none":
        missing = _missing_reply(intent)
        return missing if not missing.is_empty() else None
    if res.status == "ambiguous":
        return _ask_which(tracker, intent, res, actor, text, msg_link)

    applied = [
        apply_one(
            tracker,
            intent.kind,
            post,
            actor,
            stage=res.stages.get(post.id, intent.stage),
            new_date=intent.new_date,
            new_weekday=intent.new_weekday,
            msg_link=msg_link,
            text=text,
        )
        for post in res.posts
    ]
    reply = build_reply(tracker, intent.kind, applied, res.confident, actor)
    return reply if not reply.is_empty() else None


def _handle_assign(
    tracker: Tracker, intent: Intent, actor: Actor, reply_post_ids: list[int] | None, text: str
) -> Reply | None:
    target = tracker.team.find(intent.target or "")
    if target is None or target.role == Role.BOSS:
        return None
    if (target.id is not None and target.id == actor.id) or (
        target.username and target.username == actor.username
    ):
        return None
    res = resolve(
        tracker, Intent(IntentKind.CLAIM, intent.ref), target, reply_post_ids=reply_post_ids
    )
    if res.status != "resolved":
        return None
    posts = [p for p in res.posts if not p.assigned or p.owned_by(target)]
    if not posts:
        return None
    payload = {
        "post_ids": [p.id for p in posts],
        "target_id": target.id,
        "target_username": target.username,
        "by": actor.name,
    }
    pending_id = tracker.state.create_pending("assign", payload, target.id, tracker.now())
    what = ", ".join(fmt.line(p) for p in posts)
    return Reply(
        f"{tracker.team.mention(target)}, {escape(actor.name)} предлагает тебе взять: {what}. "
        "Берёшь?",
        [[Button("Беру", f"assign_ok:{pending_id}"), Button("Не беру", f"assign_no:{pending_id}")]],
        post_ids=[p.id for p in posts],
        kind="ask",
    )


# --- кнопки ------------------------------------------------------------


@dataclass
class CallbackResult:
    reply: Reply | None = None  # новое сообщение в чат
    edit_text: str | None = None  # заменить текст сообщения с кнопкой (кнопки убираются)
    alert: str | None = None  # всплывающее уведомление тому, кто нажал
    remove_buttons: bool = False


def handle_callback(
    tracker: Tracker,
    data: str,
    actor: Actor,
    *,
    parsed_kp: KpParseResult | None = None,
) -> CallbackResult:
    parts = data.split(":")
    action = parts[0]
    try:
        if action == "claim":
            post = tracker.get(int(parts[1]))
            if post is None:
                return CallbackResult(alert="Пост не найден")
            applied = apply_one(tracker, IntentKind.CLAIM, post, actor, source="button")
            if applied.result.code == "taken_by_other":
                return CallbackResult(alert=f"Уже взял {applied.result.previous_assignee}")
            reply = build_reply(tracker, IntentKind.CLAIM, [applied], True, actor)
            return CallbackResult(reply=reply if not reply.is_empty() else None)
        if action == "steal":
            post = tracker.get(int(parts[1]))
            if post is None:
                return CallbackResult(alert="Пост не найден")
            applied = apply_one(tracker, IntentKind.CLAIM, post, actor, source="button", steal=True)
            reply = build_reply(tracker, IntentKind.CLAIM, [applied], True, actor)
            return CallbackResult(reply=reply, remove_buttons=True)
        if action == "keep":
            return CallbackResult(edit_text="Оставлено как есть.")
        if action == "undo":
            result = tracker.undo(int(parts[1]), actor)
            texts = {
                "ok": "↩️ Отменено.",
                "already_undone": "Это уже отменено.",
                "changed_since": "После этого пост менялся — сначала отмените последнее действие.",
                "forbidden": "Отменить может тот, кто сделал, или ответственный за проект.",
                "not_undoable": "Это действие отменить нельзя.",
                "not_found": "Не нашёл, что отменять.",
            }
            message = texts.get(result.code, "Не получилось отменить.")
            if result.ok:
                return CallbackResult(
                    edit_text=f"{message} {fmt.label(result.post)}" if result.post else message
                )
            return CallbackResult(alert=message)
        if action == "stage":
            post = tracker.get(int(parts[1]))
            stage = Stage(int(parts[2]))
            if post is None:
                return CallbackResult(alert="Пост не найден")
            applied = apply_one(
                tracker, IntentKind.STAGE, post, actor, stage=stage, source="button"
            )
            reply = build_reply(tracker, IntentKind.STAGE, [applied], False, actor)
            return CallbackResult(
                reply=reply if not reply.is_empty() else None, remove_buttons=False
            )
        if action == "pick":
            return _handle_pick(tracker, int(parts[1]), int(parts[2]), actor)
        if action == "assign_ok" or action == "assign_no":
            return _handle_assign_answer(tracker, action, int(parts[1]), actor)
        if action == "kp":
            return _handle_kp_button(tracker, parts[1], parts[2], actor, parsed_kp)
        if action == "vanish":
            return _handle_vanish(tracker, parts[1], int(parts[2]), actor)
    except (IndexError, ValueError):
        return CallbackResult(alert="Кнопка устарела")
    return CallbackResult(alert="Кнопка устарела")


def _handle_pick(tracker: Tracker, pending_id: int, post_id: int, actor: Actor) -> CallbackResult:
    pending = tracker.state.get_pending(pending_id)
    if pending is None:
        return CallbackResult(alert="Вопрос уже закрыт", remove_buttons=True)
    _, payload, owner_id = pending
    if owner_id is not None and actor.id != owner_id and actor.role != Role.RESPONSIBLE:
        return CallbackResult(alert="Выбирает тот, кто писал сообщение")
    tracker.state.resolve_pending(pending_id)
    if post_id == 0:
        return CallbackResult(edit_text="Хорошо, ничего не меняю.")
    post = tracker.get(post_id)
    if post is None:
        return CallbackResult(edit_text="Пост не найден.")
    kind = IntentKind(payload["kind"])
    stage = Stage(payload["stage"]) if payload.get("stage") is not None else None
    stage = stage or effective_stage(kind, post, None)
    applied = apply_one(
        tracker,
        kind,
        post,
        actor,
        stage=stage,
        new_date=date.fromisoformat(payload["new_date"]) if payload.get("new_date") else None,
        new_weekday=payload.get("new_weekday"),
        msg_link=payload.get("msg_link"),
        text=payload.get("text"),
        source="button",
    )
    reply = build_reply(tracker, kind, [applied], False, actor)
    return CallbackResult(reply=reply if not reply.is_empty() else None, edit_text="Понял.")


def _handle_assign_answer(
    tracker: Tracker, action: str, pending_id: int, actor: Actor
) -> CallbackResult:
    pending = tracker.state.get_pending(pending_id)
    if pending is None:
        return CallbackResult(alert="Вопрос уже закрыт", remove_buttons=True)
    _, payload, _ = pending
    is_target = (actor.id is not None and actor.id == payload.get("target_id")) or (
        bool(actor.username) and actor.username == payload.get("target_username")
    )
    if not is_target:
        return CallbackResult(alert="Отвечает тот, кому предложили")
    tracker.state.resolve_pending(pending_id)
    if action == "assign_no":
        return CallbackResult(edit_text=f"{escape(actor.name)} не берёт.")
    applied = [
        apply_one(tracker, IntentKind.CLAIM, post, actor, source="button")
        for post in tracker.by_ids(payload["post_ids"])
    ]
    reply = build_reply(tracker, IntentKind.CLAIM, applied, True, actor)
    return CallbackResult(reply=reply, edit_text="Договорились.")


def _handle_kp_button(
    tracker: Tracker, action: str, month_key: str, actor: Actor, parsed_kp: KpParseResult | None
) -> CallbackResult:
    month = month_from_key(month_key)
    name = fmt.MONTHS_NOM[month.month - 1]
    now = tracker.now()
    if action == "own":
        tracker.state.set_kp_owner(month, actor)
        return CallbackResult(
            reply=Reply(f"📅 КП на {name}: сборку ведёт {escape(actor.name)}."), remove_buttons=True
        )
    if action == "ok":
        if actor.role != Role.RESPONSIBLE:
            return CallbackResult(alert="Финальный ок даёт ответственный за проект")
        tracker.state.mark_kp_ok(month, now)
        return CallbackResult(
            reply=Reply(f"📅 КП на {name}: финальный ок записан."), remove_buttons=True
        )
    if action == "shown":
        tracker.state.mark_kp_shown(month, now)
        return CallbackResult(
            reply=Reply(f"📅 КП на {name}: показано клиенту. Ждём комментарии."),
            remove_buttons=True,
        )
    return CallbackResult(alert="Кнопка устарела")


def _handle_vanish(tracker: Tracker, action: str, post_id: int, actor: Actor) -> CallbackResult:
    post = tracker.get(post_id)
    if post is None:
        return CallbackResult(edit_text="Пост не найден.")
    if action == "remove":
        tracker.cancel(post_id, actor, remember=False, undoable=False)
        return CallbackResult(edit_text=f"Убрано из трекера: {fmt.label(post)}.")
    # keep: пост остаётся в трекере как добавленный из чата и больше не сверяется с КП
    tracker._apply(
        post_id, {"source": "chat"}, kind="keep", actor=actor, source="button", undoable=False
    )
    return CallbackResult(edit_text=f"Оставлено в трекере: {fmt.label(post)} (нет в КП).")
