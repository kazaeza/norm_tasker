"""Помощник: отвечает на вопросы о постах по данным трекера.

В Gemini уходят вопрос и краткая сводка: посты на три недели вперёд (номер, дата, рубрика, тема,
этап, кто взял, ближайший срок), строка про КП и список команды. Переписка чата, тексты постов
и комментарии клиента не отправляются.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta

from norm_tasker import fmt
from norm_tasker.ai.gemini import GeminiClient
from norm_tasker.bot.telegram import clip_message
from norm_tasker.config import Role
from norm_tasker.digest.kp import kp_status, summary_line
from norm_tasker.kp.models import KpParseResult
from norm_tasker.tracker.models import Actor, Post
from norm_tasker.tracker.service import Tracker
from norm_tasker.tracker.stages import LABEL, NEXT_STEP, Stage

BACK_DAYS = 3  # недавно вышедшие и пропущенные посты
AHEAD_DAYS = 21
MAX_POSTS = 60
MAX_QUESTION = 600
ROLE_NAMES = {
    Role.COPYWRITER: "копирайтер",
    Role.RESPONSIBLE: "ответственный за проект",
    Role.BOSS: "руководитель",
}

SYSTEM = """Ты — бот SMM-команды, которая ведёт посты из контент-плана (КП). Тебя спрашивают \
в рабочем чате.

Правила:
- Отвечай по-русски, коротко и простыми словами: обычно 2–6 строк, не больше 12.
- Используй только данные из блока «ДАННЫЕ». Не выдумывай посты, имена и даты. Если ответа в \
данных нет, так и скажи.
- Называй посты номером и датой, например «№12, пт 02.10»: по номеру человек найдёт пост командой \
/post 12.
- Не используй разметку Markdown: без звёздочек, решёток и обратных кавычек. Перечисление начинай \
строками с «• ».
- Ты ничего не меняешь в трекере. Чтобы взять пост или сдвинуть этап, человек пишет в чат обычной \
фразой, например «беру пост на пятницу» или «текст готов, отправил клиенту».
- Команды бота: /week, /today, /my, /free, /design, /kp, /board, /post 12, /help.
- Если вопрос не про посты и работу команды, вежливо скажи, что помогаешь только с постами."""


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


def build_snapshot(tracker: Tracker, parsed_kp: KpParseResult | None, asker: Actor | None) -> str:
    now = tracker.now()
    today = now.date()
    zone = tracker.settings.timezone
    lines = [f"Сегодня: {fmt.wd_date(today)}.{today.year}, сейчас {now:%H:%M} ({zone})."]
    if asker is not None:
        lines.append(f"Спрашивает: {asker.name} ({ROLE_NAMES[asker.role]}).")
    team = ", ".join(f"{m.name} — {ROLE_NAMES[m.role]}" for m in tracker.team.members())
    if team:
        lines.append(f"Команда: {team}.")

    posts = tracker.open_posts(today, back_days=BACK_DAYS, ahead_days=AHEAD_DAYS)
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
    return "\n".join(lines)


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

    async def answer(
        self, question: str, asker: Actor | None, parsed_kp: KpParseResult | None
    ) -> str:
        snapshot = build_snapshot(self.tracker, parsed_kp, asker)
        text = await self.client.ask(SYSTEM, f"ДАННЫЕ\n{snapshot}\n\nВОПРОС\n{question}")
        return to_html(text)

    async def ping(self) -> str:
        return await self.client.ping()
