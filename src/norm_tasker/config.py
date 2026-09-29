"""Настройки бота.

Секреты и идентификаторы таблиц берутся из переменных окружения, всё остальное —
из config.yaml (команда, сроки, время напоминаний). Оба хранятся на сервере, а не
в репозитории.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, time
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator


def _parse_time(value: object) -> object:
    """Принимает «09:30», time и целое число минут.

    YAML 1.1 превращает незакавыченное 9:30 в число 570 (шестидесятеричная запись),
    поэтому целые числа читаем как минуты от полуночи.
    """
    if isinstance(value, bool):
        raise ValueError("время должно быть в формате ЧЧ:ММ")
    if isinstance(value, int):
        if not 0 <= value < 24 * 60:
            raise ValueError("время должно быть в формате ЧЧ:ММ")
        return time(value // 60, value % 60)
    if isinstance(value, str):
        try:
            hours, minutes = value.strip().split(":")
            return time(int(hours), int(minutes))
        except ValueError as exc:
            raise ValueError(f"время должно быть в формате ЧЧ:ММ, а не {value!r}") from exc
    return value


Hhmm = Annotated[time, BeforeValidator(_parse_time)]


class Role(StrEnum):
    COPYWRITER = "copywriter"  # берёт посты и пишет тексты
    RESPONSIBLE = "responsible"  # ответственный за проект: дизайн, показ клиенту, эскалации
    BOSS = "boss"  # руководитель: бот его не тегает


class Strict(BaseModel):
    # Опечатка в config.yaml должна давать понятную ошибку, а не молча игнорироваться.
    model_config = ConfigDict(extra="forbid")


class Member(Strict):
    name: str
    username: str | None = None
    user_id: int | None = None
    role: Role = Role.COPYWRITER

    @field_validator("username")
    @classmethod
    def _clean_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lstrip("@").lower()
        return value or None


class DeadlineSettings(Strict):
    """Сроки поста. Дни считаются рабочими; D — дата выхода."""

    take_days_before: int = 4  # D−4: пост должен быть взят
    text_days_before: int = 2  # D−2: текст готов и показан клиенту
    show_days_before: int = 1  # D−1: готовый пост с дизайном показан клиенту
    day_end: Hhmm = time(18, 0)  # конец рабочего дня (Москва)
    text_ok_warn: Hhmm = time(11, 0)  # D−1: если ока по тексту нет, пора напомнить клиенту
    text_ok_hard: Hhmm = time(13, 0)  # D−1: позже дизайн не успевает
    handoff_from: Hhmm = time(9, 0)  # D−1: окно, когда лучше отдать дизайнеру
    handoff_to: Hhmm = time(10, 0)
    handoff_hard: Hhmm = time(13, 0)  # D−1: крайний срок передачи (Москва)
    final_ok_check: Hhmm = time(12, 0)  # D: проверка финального ока и публикации
    design_hours_min: float = 2
    design_hours_max: float = 3


class ScheduleSettings(Strict):
    """Время напоминаний (Москва). Каждое срабатывает один раз в сутки."""

    summary: Hhmm = time(9, 30)
    take_reminder: Hhmm = time(16, 0)  # D−4
    text_reminder: Hhmm = time(12, 0)  # D−2
    text_overdue: Hhmm = time(18, 0)  # D−2
    ok_nudge: Hhmm = time(11, 0)  # D−1
    handoff_deadline: Hhmm = time(13, 0)  # D−1
    show_reminder: Hhmm = time(16, 0)  # D−1
    show_overdue: Hhmm = time(18, 0)  # D−1
    final_reminder: Hhmm = time(12, 0)  # D
    weekly_report: Hhmm = time(17, 0)  # пятница
    kp_reminder: Hhmm = time(12, 0)  # напоминание об оке КП
    kp_overdue: Hhmm = time(18, 0)
    # Сколько после назначенного времени бот ещё отправит пропущенное напоминание
    # (например, если его перезапустили).
    catch_up_minutes: int = 90
    summary_catch_up_minutes: int = 180


class KpSettings(Strict):
    """КП на следующий месяц."""

    show_day: int = 25  # число месяца, когда КП показывают клиенту
    start_workdays_before_show: int = 4  # старт сборки
    ok_workdays_before_show: int = 1  # финальный ок ответственного — до конца этого дня
    horizon_days: int = 60


class Settings(Strict):
    timezone: str = "Europe/Moscow"
    designer_timezone: str = "Asia/Yekaterinburg"
    chat_id: int | None = None
    thread_id: int | None = None  # тема группы, куда бот пишет; без тем — пусто
    team: list[Member] = []
    # Кого считать участником, если его нет в списке team. None — игнорировать.
    unknown_role: Role | None = Role.COPYWRITER
    # Имена авторов комментариев в Google (как они показаны в файле).
    client_authors: list[str] = []
    team_authors: list[str] = []
    deadlines: DeadlineSettings = DeadlineSettings()
    schedule: ScheduleSettings = ScheduleSettings()
    kp: KpSettings = KpSettings()
    soft_start_days: int = 14  # первые дни бот только пишет саммари и ведёт доску
    poll_kp_seconds: int = 300
    poll_docs_seconds: int = 900
    history_days: int = 3  # посты старше не подтягиваем из КП
    horizon_days: int = 60  # посты дальше вперёд не подтягиваем
    extra_days_off: list[date] = []  # дополнительные нерабочие дни
    extra_workdays: list[date] = []  # рабочие дни, которых нет в производственном календаре
    weekend_reminders: bool = False  # писать ли в выходные

    @field_validator("timezone", "designer_timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError(f"неизвестный часовой пояс {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Env:
    """Секреты и пути, заданные окружением."""

    bot_token: str | None
    data_dir: Path
    config_path: Path
    google_credentials: Path | None
    kp_spreadsheet_id: str | None
    tracker_spreadsheet_id: str | None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tracker.db"


def load_env(environ: dict[str, str] | None = None) -> Env:
    env = dict(os.environ if environ is None else environ)
    data_dir = Path(env.get("DATA_DIR") or "data")
    config_path = Path(env.get("CONFIG_PATH") or data_dir / "config.yaml")
    creds = env.get("GOOGLE_APPLICATION_CREDENTIALS")
    return Env(
        bot_token=env.get("BOT_TOKEN") or None,
        data_dir=data_dir,
        config_path=config_path,
        google_credentials=Path(creds) if creds else None,
        kp_spreadsheet_id=env.get("KP_SPREADSHEET_ID") or None,
        tracker_spreadsheet_id=env.get("TRACKER_SPREADSHEET_ID") or None,
    )


def load_settings(path: Path) -> Settings:
    if not path.exists():
        raise FileNotFoundError(
            f"Не найден файл настроек {path}. Скопируйте config.example.yaml и заполните его."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(raw)
