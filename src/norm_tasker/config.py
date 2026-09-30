"""Настройки бота.

Секреты и идентификаторы таблиц берутся из переменных окружения, всё остальное —
из config.yaml (команда, сроки, время напоминаний). Оба хранятся на сервере, а не
в репозитории. Если файл положить некуда (например, в облачном хостинге), текст config.yaml
можно передать переменной CONFIG_YAML или CONFIG_B64 (тот же текст в base64), а номер
чата — переменными CHAT_ID и THREAD_ID.
"""

from __future__ import annotations

import base64
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
    # Как в чате зовут людей клиента («отправил Алисе текст»); по этим именам бот понимает, что
    # текст или пост ушёл клиенту.
    client_names: list[str] = []
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
    # Статусы КП → этапы (TAKEN, TEXT_SHOWN, TEXT_OK, AT_DESIGNER, DESIGN_READY, SHOWN_DESIGN,
    # FINAL_OK, PUBLISHED). Дополняет и переопределяет стандартное соответствие.
    kp_statuses: dict[str, str] = {}

    @field_validator("timezone", "designer_timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError(f"неизвестный часовой пояс {value!r}") from exc
        return value

    @field_validator("kp_statuses")
    @classmethod
    def _known_stages(cls, value: dict[str, str]) -> dict[str, str]:
        from norm_tasker.tracker.stages import Stage

        for status, stage in value.items():
            if stage.upper() not in Stage.__members__:
                raise ValueError(f"статус «{status}»: неизвестный этап {stage!r}")
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def status_stages(self) -> dict:
        """Соответствие статусов КП этапам: стандартное плюс настройки из config.yaml."""
        from norm_tasker.tracker.stages import DEFAULT_KP_STATUS_STAGES, Stage, normalize

        mapping = dict(DEFAULT_KP_STATUS_STAGES)
        for status, stage in self.kp_statuses.items():
            mapping[normalize(status)] = Stage[stage.upper()]
        return mapping


@dataclass(frozen=True)
class Env:
    """Секреты и пути, заданные окружением."""

    bot_token: str | None
    data_dir: Path
    config_path: Path
    google_credentials: Path | None
    kp_spreadsheet_id: str | None
    tracker_spreadsheet_id: str | None
    config_text: str | None = None  # текст настроек из CONFIG_YAML / CONFIG_B64
    chat_id: int | None = None  # CHAT_ID и THREAD_ID важнее значений из настроек
    thread_id: int | None = None
    telegram_api_url: str | None = None  # свой адрес Bot API, если api.telegram.org недоступен
    gemini_key: str | None = None  # GEMINI_API (или GEMINI_API_KEY): без ключа ИИ выключен
    gemini_model: str | None = None  # GEMINI_MODEL: не задана — бот подберёт модель сам
    gemini_url: str | None = None  # GEMINI_URL: адрес API, если ключ выдал посредник
    ai_enabled: bool = True  # AI_ENABLED=0 выключает ИИ, не убирая ключ

    @property
    def ai_on(self) -> bool:
        return self.ai_enabled and bool(self.gemini_key)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tracker.db"

    @property
    def has_config(self) -> bool:
        return self.config_text is not None or self.config_path.exists()


OFF_WORDS = {"0", "false", "no", "off", "нет", "выкл"}


def _env_int(env: dict[str, str], name: str) -> int | None:
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} должен быть целым числом, а не {raw!r}") from exc


def _env_config_text(env: dict[str, str]) -> str | None:
    text = env.get("CONFIG_YAML")
    if text and text.strip():
        return text
    encoded = env.get("CONFIG_B64")
    if encoded and encoded.strip():
        try:
            return base64.b64decode("".join(encoded.split()), validate=True).decode("utf-8")
        except ValueError as exc:  # и не-base64, и не UTF-8: оба — подклассы ValueError
            raise ValueError(
                "CONFIG_B64 не читается: там должен быть текст config.yaml, закодированный в base64"
            ) from exc
    return None


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
        config_text=_env_config_text(env),
        chat_id=_env_int(env, "CHAT_ID"),
        thread_id=_env_int(env, "THREAD_ID"),
        telegram_api_url=env.get("TELEGRAM_API_URL") or None,
        gemini_key=(env.get("GEMINI_API") or env.get("GEMINI_API_KEY") or "").strip() or None,
        gemini_model=(env.get("GEMINI_MODEL") or "").strip() or None,
        gemini_url=(env.get("GEMINI_URL") or "").strip() or None,
        ai_enabled=(env.get("AI_ENABLED") or "1").strip().lower() not in OFF_WORDS,
    )


def _parse_raw(text: str) -> dict:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Настройки не читаются как YAML: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            "Настройки должны быть строками «ключ: значение», как в config.example.yaml"
        )
    return raw


def load_settings(path: Path) -> Settings:
    if not path.exists():
        raise FileNotFoundError(_missing_config(path))
    return Settings.model_validate(_parse_raw(path.read_text(encoding="utf-8")))


def _missing_config(path: Path) -> str:
    return (
        f"Не найден файл настроек {path}. Скопируйте config.example.yaml и заполните его "
        "(или передайте текст настроек в переменной CONFIG_YAML)."
    )


def load_settings_for(env: Env) -> Settings:
    """Настройки из переменной CONFIG_YAML/CONFIG_B64 или из файла; CHAT_ID и THREAD_ID сверху."""
    if env.config_text is not None:
        raw = _parse_raw(env.config_text)
    elif env.config_path.exists():
        raw = _parse_raw(env.config_path.read_text(encoding="utf-8"))
    else:
        raise FileNotFoundError(_missing_config(env.config_path))
    if env.chat_id is not None:
        raw["chat_id"] = env.chat_id
    if env.thread_id is not None:
        raw["thread_id"] = env.thread_id
    return Settings.model_validate(raw)
