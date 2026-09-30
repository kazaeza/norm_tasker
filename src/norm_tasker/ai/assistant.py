"""Помощник: понимает сообщения чата и отвечает на вопросы о постах по данным трекера.

В Gemini уходят сообщение, краткая сводка по постам (номер, дата, рубрика, тема, этап, кто взял,
ближайший срок), строка про КП, список команды и последние сообщения чата. Ответ — JSON: ответ
человеку или действия над трекером, которые потом проверяет `interpreter`.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from norm_tasker import fmt
from norm_tasker.ai.gemini import GeminiClient
from norm_tasker.ai.interpreter import Interpretation, parse_interpretation
from norm_tasker.bot.telegram import clip_message
from norm_tasker.chat.log import ChatLine
from norm_tasker.config import Role
from norm_tasker.digest.kp import kp_status, summary_line
from norm_tasker.kp.models import KpParseResult
from norm_tasker.tracker.models import Actor, Post
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import LABEL, NEXT_STEP, Stage

BACK_DAYS = 3  # недавно вышедшие посты
OVERDUE_DAYS = 10  # не вышедшие посты, которым срок уже вышел
AHEAD_DAYS = 21
MAX_POSTS = 60
MAX_FOCUS = 8  # ответ на сообщение бота о таком числе постов или меньше считаем вопросом о них
MAX_QUESTION = 600
ROLE_NAMES = {
    Role.COPYWRITER: "копирайтер",
    Role.RESPONSIBLE: "ответственный за проект",
    Role.BOSS: "руководитель",
}

SYSTEM = """Ты — бот SMM-команды, которая ведёт посты по контент-плану (КП). Ты сидишь в рабочем \
чате как коллега: отвечаешь на вопросы и записываешь в трекер, что сделано по постам.

Ты получаешь блоки: «ДАННЫЕ» (сегодняшняя дата, автор сообщения, команда, посты с номерами и \
этапами), «РЕЖИМ», иногда «ПОСЛЕДНИЕ СООБЩЕНИЯ ЧАТА» и «СООБЩЕНИЕ, НА КОТОРОЕ ОТВЕТИЛИ», и само \
сообщение.

Ответь ОДНИМ JSON-объектом, без пояснений и без обрамления кодом:
{"kind": "answer", "text": "...", "confidence": "high", "actions": []}

kind:
- answer — человек спросил тебя или поговорил с тобой: ответ в поле text;
- action — человек сообщил о сделанном или попросил записать: действия в поле actions, text пустой;
- clarify — ясно, что надо записать, но неясно, какой пост или что именно: один короткий \
уточняющий вопрос в поле text;
- ignore — сообщение не для тебя и не про посты: text пустой.

Действия (type):
- claim — автор берёт пост: {"type": "claim", "posts": [12]}
- release — автор отказывается от своего поста: {"type": "release", "posts": [12]}
- stage — пост перешёл на этап: {"type": "stage", "posts": [12], "stage": "at_designer"}
- rollback — клиент вернул на правки, этап откатывается назад: \
{"type": "rollback", "posts": [12], "stage": "taken"}
- move — пост перенесён на другую дату: {"type": "move", "posts": [12], "date": "2026-10-20"}
- not_post — строка КП на самом деле не пост: {"type": "not_post", "posts": [12]}
- add_post — новый пост, которого нет в КП: \
{"type": "add_post", "date": "2026-10-20", "topic": "тема"}
Даты в ответе пишутся как ГГГГ-ММ-ДД. «На понедельник» — ближайший понедельник после нынешней \
даты выхода поста.

Этапы (stage), по порядку; в скобках — как этап назван в данных:
taken — пост взят, автор пишет текст («взят, пишется текст»)
text_shown — текст готов и показан клиенту («текст у клиента»)
text_ok — клиент одобрил текст («текст одобрен»)
at_designer — документ передан дизайнеру («у дизайнера»)
design_ready — дизайн готов («дизайн готов»)
shown_design — готовый пост с дизайном показан клиенту («пост у клиента»)
final_ok — финальный ок клиента («финальный ок»)
published — пост опубликован или стоит в отложенных («опубликован»)

Как работать:
1. Номера постов бери только из «ДАННЫХ». Пост определяй по номеру, дате, дню недели, рубрике или \
теме. Если сообщение — ответ на сообщение бота про пост, речь о нём. Если подходят несколько \
постов, а по смыслу не понять, какой, — clarify.
2. Записывай только то, что уже случилось или о чём человек просит записать: «отправила \
клиенту», «клиент одобрил текст», «беру пост про свидания», «перенесите пост на понедельник». \
Планы, вопросы и рассуждения («завтра отправлю», «кто взял пост?», «когда срок?») — не действия.
3. Действие записывается на автора сообщения. Если человек пишет за другого («Вася взял пост»), \
ничего не записывай: подскажи, что Вася может написать сам. О клиенте («клиент одобрил текст», \
«Алиса прислала правки») может сообщить любой участник.
4. Этапы идут только вперёд. Назад — rollback, и только если человек говорит, что клиент вернул \
текст или дизайн на правки.
5. «Готово», «сделал», «написал» от автора поста, который сейчас на этапе «взят, пишется текст», \
значит text_shown. Если неясно, что именно готово, — clarify.
6. «Клиент ок» на этапе «текст у клиента» — text_ok; на этапе «пост у клиента» — final_ok.
7. Если в «РЕЖИМЕ» сказано, что к боту не обращались, отвечай только kind action с confidence high \
или ignore. Никаких answer и clarify, никаких догадок.
8. confidence high — только если и пост, и этап (или дата) однозначно следуют из сообщения.
9. Отвечай по-русски, коротко и просто, как коллега: 1–5 строк, на «вы». Без Markdown: без \
звёздочек, решёток и обратных кавычек. Перечисление начинай строками с «• ».
10. Используй только данные из «ДАННЫХ». Не выдумывай посты, имена и даты. Если ответа в данных \
нет, так и скажи.
11. Ты сам ничего не переносишь и не удаляешь, кроме действий из списка выше. Если просят \
что-то другое (напомнить, написать клиенту), вежливо скажи, что не умеешь.
12. Если вопрос не про посты и работу команды, вежливо скажи, что помогаешь только с постами.
13. Команды бота: /week, /today, /my, /free, /design, /kp, /board, /post 12, /undo, /help."""


@dataclass
class Request:
    """Сообщение из чата и всё, что нужно, чтобы его понять."""

    text: str  # без «@бота»
    asker: Actor | None
    addressed: bool  # к боту обратились: по имени, ответом на его сообщение или пришёл вопрос
    focus_ids: list[int] = field(default_factory=list)  # посты сообщения бота, на которое ответили
    recent: list[ChatLine] = field(default_factory=list)  # что писали до этого
    quoted: str | None = None  # текст сообщения, на которое ответили
    quoted_by: str | None = None  # кто его написал


def ask_text(text: str, bot_username: str | None) -> str:
    """Вопрос без обращения к боту: «@бот чё по задачам?» → «чё по задачам?»."""
    if bot_username:
        text = re.sub(rf"@{re.escape(bot_username)}\b", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())[:MAX_QUESTION]


def _post_line(tracker: Tracker, post: Post, now: datetime) -> str:
    author = post.assignee_name or "никто не взял"
    line = (
        f"№{post.id} | {fmt.wd_date(post.publish_date)} | {post.rubric or '—'} | "
        f"«{fmt.clip(post.title, 80)}» | {LABEL[post.stage]} | автор: {author}"
    )
    if post.stage < Stage.PUBLISHED:
        due = tracker.chain(post).stage_due(Stage(post.stage + 1))
        if due is not None:
            late = " (срок прошёл)" if due < now else ""
            line += f" | дальше: {NEXT_STEP[post.stage]} до {fmt.dt_short(due)}{late}"
    return line


def build_snapshot(
    tracker: Tracker,
    parsed_kp: KpParseResult | None,
    asker: Actor | None,
    focus_ids: list[int] | None = None,
    asker_label: str = "Спрашивает",
) -> str:
    now = tracker.now()
    today = now.date()
    zone = tracker.settings.timezone
    lines = [f"Сегодня: {fmt.wd_date(today)}.{today.year}, сейчас {now:%H:%M} ({zone})."]
    if asker is not None:
        lines.append(f"{asker_label}: {asker.name} ({ROLE_NAMES[asker.role]}).")
    team = ", ".join(f"{m.name} — {ROLE_NAMES[m.role]}" for m in tracker.team.members())
    if team:
        lines.append(f"Команда: {team}.")
    if tracker.settings.client_names:
        lines.append(f"Люди клиента: {', '.join(tracker.settings.client_names)}.")

    posts = tracker.open_posts(today, back_days=OVERDUE_DAYS, ahead_days=AHEAD_DAYS)
    lines.append("")
    lines.append(
        "Посты, которые ещё не вышли "
        "(номер | дата выхода | рубрика | тема | этап | автор | что дальше):"
    )
    lines.extend(_post_line(tracker, post, now) for post in posts[:MAX_POSTS])
    if len(posts) > MAX_POSTS:
        lines.append(f"… и ещё {len(posts) - MAX_POSTS} постов позже.")
    if not posts:
        lines.append("Таких постов нет.")

    recent = [
        p
        for p in tracker.posts_between(today - timedelta(days=BACK_DAYS), today)
        if p.stage == Stage.PUBLISHED and not p.cancelled
    ]
    if recent:
        lines.append("")
        lines.append("Недавно вышли:")
        lines.extend(
            f"№{p.id} | {fmt.wd_date(p.publish_date)} | «{fmt.clip(p.title, 80)}»" for p in recent
        )

    plan = summary_line(kp_status(tracker, today, parsed_kp), today)
    if plan:
        lines.append("")
        lines.append(plan)

    # Ответ на сообщение бота: называем посты, о которых оно было.
    # Ответ на длинный список ни к какому посту не привязан, он и так есть в сводке выше.
    few = focus_ids if focus_ids and len(focus_ids) <= MAX_FOCUS else []
    focus = [p for p in tracker.by_ids(few) if not p.cancelled]
    if focus:
        lines.append("")
        lines.append("Вопрос задан в ответ на сообщение бота, в котором речь шла об этих постах:")
        lines.extend(_post_line(tracker, post, now) for post in focus)
    return "\n".join(lines)


def build_prompt(snapshot: str, request: Request) -> str:
    """Что уходит в Gemini: данные, режим, контекст чата и само сообщение."""
    if request.addressed:
        mode = "К боту обратились напрямую: отвечай на вопрос или записывай, что просят."
    else:
        mode = (
            "К боту не обращались: это обычное сообщение из рабочего чата. Записывай только "
            "однозначный отчёт о постах (kind action, confidence high), иначе ignore."
        )
    parts = [f"ДАННЫЕ\n{snapshot}", f"РЕЖИМ\n{mode}"]
    if request.recent:
        chat = "\n".join(f"[{line.at:%H:%M}] {line.author}: {line.text}" for line in request.recent)
        parts.append(f"ПОСЛЕДНИЕ СООБЩЕНИЯ ЧАТА (старые первыми)\n{chat}")
    if request.quoted:
        who = request.quoted_by or "кто-то"
        parts.append(f"СООБЩЕНИЕ, НА КОТОРОЕ ОТВЕТИЛИ ({who})\n{request.quoted}")
    label = "ВОПРОС" if request.addressed else "СООБЩЕНИЕ ИЗ ЧАТА"
    parts.append(f"{label}\n{request.text}")
    return "\n\n".join(parts)


def to_html(text: str) -> str:
    """Ответ модели → безопасный HTML для Telegram: без чужой разметки, с жирным и точками."""
    text = html.escape(text.strip().replace("`", ""), quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^[ \t]*[*\-•][ \t]+", "• ", text)
    return clip_message(text)


class Assistant:
    def __init__(self, client: GeminiClient, tracker: Tracker) -> None:
        self.client = client
        self.tracker = tracker

    @property
    def model(self) -> str | None:
        return self.client.model

    async def interpret(self, request: Request, parsed_kp: KpParseResult | None) -> Interpretation:
        """Как понять сообщение: ответить, записать в трекер, уточнить или пропустить."""
        label = "Спрашивает" if request.addressed else "Пишет"
        snapshot = build_snapshot(
            self.tracker, parsed_kp, request.asker, request.focus_ids, asker_label=label
        )
        raw = await self.client.ask(SYSTEM, build_prompt(snapshot, request), json_mode=True)
        return parse_interpretation(raw, self.tracker.today())

    async def ping(self) -> str:
        return await self.client.ping()
