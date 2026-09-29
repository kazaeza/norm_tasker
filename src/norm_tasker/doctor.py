"""Проверка настройки: что подключено, чего не хватает. Запуск: `python -m norm_tasker doctor`."""

from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner

from norm_tasker.calendar_ru import build_calendar
from norm_tasker.config import Env, Role, Settings
from norm_tasker.deadlines import kp_timeline, next_month
from norm_tasker.fmt import wd_date
from norm_tasker.google.client import GoogleClient, GoogleError
from norm_tasker.kp.parser import parse_kp

OK, BAD, WARN = "✅", "❌", "⚠️ "


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def ok(self, text: str) -> None:
        print(f"{OK} {text}")

    def bad(self, text: str, hint: str = "") -> None:
        self.failed += 1
        print(f"{BAD} {text}")
        if hint:
            print(f"   → {hint}")

    def warn(self, text: str, hint: str = "") -> None:
        print(f"{WARN}{text}")
        if hint:
            print(f"   → {hint}")


async def check_telegram(env: Env, settings: Settings, report: Report) -> None:
    if not env.bot_token:
        report.bad("BOT_TOKEN не задан", "создайте бота у @BotFather и положите токен в .env")
        return
    bot = Bot(env.bot_token)
    try:
        me = await bot.get_me()
    except TelegramAPIError as error:
        report.bad(f"Токен не принят Telegram: {error}", "проверьте BOT_TOKEN в .env")
        await bot.session.close()
        return
    report.ok(f"Бот @{me.username} отвечает")
    if me.can_read_all_group_messages:
        report.ok("Режим приватности выключен: бот видит все сообщения группы")
    else:
        report.warn(
            "Режим приватности включён",
            "если бот администратор группы, это не мешает; иначе @BotFather → /setprivacy → "
            "Disable, затем удалите бота из группы и добавьте снова",
        )
    chat_id = settings.chat_id
    if chat_id is None:
        report.bad(
            "chat_id не задан в config.yaml",
            "добавьте бота в рабочий чат и отправьте там /id — бот покажет chat_id",
        )
    else:
        try:
            chat = await bot.get_chat(chat_id)
            report.ok(f"Чат найден: «{chat.title or chat.id}»")
            member = await bot.get_chat_member(chat_id, me.id)
            if isinstance(member, ChatMemberOwner) or (
                isinstance(member, ChatMemberAdministrator) and member.can_pin_messages
            ):
                report.ok("Бот администратор и может закреплять сообщения")
            elif isinstance(member, ChatMemberAdministrator):
                report.warn(
                    "Бот администратор, но не может закреплять", "включите «Закрепление сообщений»"
                )
            else:
                report.warn(
                    "Бот не администратор чата",
                    "доску закрепить не получится; сделайте бота администратором",
                )
            if getattr(chat, "is_forum", False) and settings.thread_id is None:
                report.warn(
                    "В группе включены темы, а thread_id не задан",
                    "отправьте /id в нужной теме и впишите thread_id в config.yaml",
                )
        except TelegramAPIError as error:
            report.bad(f"Чат {chat_id} недоступен: {error}", "бот должен быть добавлен в чат")
    await bot.session.close()


def check_team(settings: Settings, report: Report) -> None:
    if not settings.team:
        report.warn("В настройках нет команды", "заполните team в config.yaml")
        return
    roles = {m.role for m in settings.team}
    names = ", ".join(f"{m.name} (@{m.username})" if m.username else m.name for m in settings.team)
    report.ok(f"Команда: {names}")
    if Role.RESPONSIBLE not in roles:
        report.bad("Не указан ответственный за проект", "role: responsible у одного из участников")
    if not settings.client_authors:
        report.warn(
            "client_authors пуст",
            "автор комментария вне списка команды считается клиентом — этого достаточно, "
            "но список team_authors нужен, чтобы не принимать за клиента своих",
        )


def check_google(env: Env, settings: Settings, report: Report) -> None:
    if not env.google_credentials or not env.google_credentials.exists():
        report.bad(
            "Ключ сервисного аккаунта не найден (GOOGLE_APPLICATION_CREDENTIALS)",
            "создайте ключ в Google Cloud и положите файл в data/service-account.json",
        )
        return
    try:
        client = GoogleClient.from_service_account(env.google_credentials)
    except Exception as error:
        report.bad(f"Ключ сервисного аккаунта не читается: {error}")
        return
    report.ok(f"Сервисный аккаунт: {client.service_account_email}")

    if not env.kp_spreadsheet_id:
        report.bad("KP_SPREADSHEET_ID не задан", "id — часть ссылки на КП между /d/ и /edit")
    else:
        try:
            info = client.file_info(env.kp_spreadsheet_id)
            report.ok(f"КП доступно: «{info.get('name')}», изменялось {info.get('modifiedTime')}")
            data = client.export_xlsx(env.kp_spreadsheet_id)
            today = datetime.now(settings.tz).date()
            parsed = parse_kp(
                data, since=today - timedelta(days=settings.history_days),
                until=today + timedelta(days=settings.horizon_days),
                status_stages=settings.status_stages(),
            )  # fmt: skip
            window = [s for s in parsed.slots if today <= s.date <= today + timedelta(days=14)]
            comments = sum(1 for c in parsed.comments if c.day)
            report.ok(
                f"Выгрузка КП разобрана: листов {len(parsed.sheets)}, постов на две недели вперёд "
                f"{len(window)}, комментариев в календаре {comments}"
            )
            for warning in parsed.warnings:
                report.warn(warning)
            if not window:
                report.warn("На ближайшие две недели постов не найдено", "проверьте шаблон листа")
        except GoogleError as error:
            report.bad(f"КП недоступно: {error}")
        except Exception as error:
            report.bad(f"Выгрузка КП не разобрана: {type(error).__name__}: {error}")

    if not env.tracker_spreadsheet_id:
        report.warn(
            "TRACKER_SPREADSHEET_ID не задан",
            "копия трекера в Google-таблице не будет вестись — бот работает и без неё",
        )
    else:
        try:
            info = client.file_info(env.tracker_spreadsheet_id)
            if (info.get("capabilities") or {}).get("canEdit"):
                report.ok(f"Внутренняя таблица «{info.get('name')}» доступна на запись")
            else:
                report.bad(
                    "Внутренняя таблица открыта только на чтение",
                    "дайте сервисному аккаунту права редактора",
                )
        except GoogleError as error:
            report.bad(f"Внутренняя таблица недоступна: {error}")


def check_calendar(env: Env, settings: Settings, report: Report) -> None:
    calendar = build_calendar(
        env.data_dir / "calendar_cache.json", settings.extra_days_off, settings.extra_workdays
    )
    today = datetime.now(settings.tz).date()
    for year in (today.year, today.year + 1):
        if calendar.has_year(year):
            report.ok(f"Производственный календарь на {year} есть")
        else:
            report.warn(
                f"Производственного календаря на {year} пока нет",
                "считаю рабочими понедельник–пятницу; бот обновит данные сам, когда календарь "
                "утвердят, а праздники можно вписать в extra_days_off",
            )
    timeline = kp_timeline(next_month(today), calendar, settings.kp)
    report.ok(
        f"КП на следующий месяц: старт {wd_date(timeline.start_date)}, ок ответственного до "
        f"{wd_date(timeline.ok_deadline)}, показ клиенту {wd_date(timeline.show_date)}"
    )


def check_storage(env: Env, report: Report) -> None:
    try:
        env.data_dir.mkdir(parents=True, exist_ok=True)
        probe = env.data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        report.ok(f"Каталог данных доступен на запись: {env.data_dir.resolve()}")
    except OSError as error:
        report.bad(f"Каталог данных недоступен: {error}", "проверьте права на папку data")


async def run_doctor(env: Env, settings: Settings) -> int:
    report = Report()
    print("Проверка настроек norm_tasker\n")
    check_storage(env, report)
    check_team(settings, report)
    check_calendar(env, settings, report)
    await check_telegram(env, settings, report)
    check_google(env, settings, report)
    print()
    if report.failed:
        print(f"Не хватает: {report.failed}. Исправьте пункты с {BAD} и запустите проверку снова.")
        return 1
    print("Всё готово к запуску.")
    return 0
