"""Команда: кто есть кто, как к кому обратиться."""

from __future__ import annotations

from datetime import datetime
from html import escape

from norm_tasker.config import Member, Role, Settings
from norm_tasker.tracker.models import Actor, Post
from norm_tasker.tracker.state import State


class Team:
    def __init__(self, settings: Settings, state: State) -> None:
        self.settings = settings
        self.state = state

    def _actor_of(self, member: Member) -> Actor:
        user_id = member.user_id
        if user_id is None and member.username:
            user_id = self.state.member_id_by_username(member.username)
        return Actor(user_id, member.name, member.username, member.role)

    def identify(
        self,
        user_id: int | None,
        username: str | None,
        full_name: str | None,
        now: datetime | None = None,
    ) -> Actor | None:
        """Кто написал в чат. None — участника нужно игнорировать."""
        username = username.lstrip("@").lower() if username else None
        member = next(
            (
                m
                for m in self.settings.team
                if (user_id is not None and m.user_id == user_id)
                or (username and m.username == username)
            ),
            None,
        )
        if member is not None:
            actor = Actor(user_id, member.name, username or member.username, member.role)
        elif self.settings.unknown_role is not None:
            actor = Actor(
                user_id, full_name or username or "Участник", username, self.settings.unknown_role
            )
        else:
            return None
        if now is not None:
            self.state.remember_member(actor, now)
        return actor

    def members(self, role: Role | None = None) -> list[Actor]:
        return [self._actor_of(m) for m in self.settings.team if role is None or m.role == role]

    def responsible(self) -> Actor | None:
        found = self.members(Role.RESPONSIBLE)
        return found[0] if found else None

    def find(self, token: str) -> Actor | None:
        """Ищет участника по @username, имени или уменьшительному («Даня» → Даниил)."""
        token = token.strip().lstrip("@").casefold().replace("ё", "е")
        if len(token) < 2:
            return None
        matches = []
        for member in self.settings.team:
            name = member.name.casefold().replace("ё", "е")
            first = name.split()[0] if name else ""
            if (
                member.username == token
                or first == token
                or (len(token) >= 3 and first[:3] == token[:3])
            ):
                matches.append(member)
        return self._actor_of(matches[0]) if len(matches) == 1 else None

    def mention(self, who: Actor | None) -> str:
        """Упоминание для сообщения в HTML: @username или ссылка на пользователя."""
        if who is None:
            return ""
        if who.username:
            return f"@{escape(who.username)}"
        if who.id is not None:
            return f'<a href="tg://user?id={who.id}">{escape(who.name)}</a>'
        return escape(who.name)

    def mention_assignee(self, post: Post) -> str:
        """Упоминание автора поста; пустая строка, если поста никто не взял."""
        if not post.assigned:
            return ""
        return self.mention(
            Actor(
                post.assignee_id, post.assignee_name or "", post.assignee_username, Role.COPYWRITER
            )
        )

    def mention_responsible(self) -> str:
        return self.mention(self.responsible())
