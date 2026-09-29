"""Запуск: `python -m norm_tasker [run|doctor|parse ФАЙЛ.xlsx]`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from norm_tasker import fmt
from norm_tasker.calendar_ru import build_calendar
from norm_tasker.config import Env, Settings, load_env, load_settings
from norm_tasker.deadlines import compute_chain
from norm_tasker.kp.parser import parse_kp
from norm_tasker.tracker.stages import LABEL, stage_from_kp_status


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram").setLevel(logging.INFO if verbose else logging.WARNING)


def cmd_run(env: Env, settings: Settings) -> int:
    from norm_tasker.bot.app import build_app

    app = build_app(env, settings)
    asyncio.run(app.run())
    return 0


def cmd_parse(
    env: Env, settings: Settings | None, path: Path, weeks: int, since: date | None
) -> int:
    """Показывает, как бот разобрал выгрузку КП и какие сроки насчитал."""
    settings = settings or Settings()
    calendar = build_calendar(None, settings.extra_days_off, settings.extra_workdays)
    start = since or datetime.now(settings.tz).date()
    end = start + timedelta(weeks=weeks)
    result = parse_kp(
        path.read_bytes(), since=start, until=end + timedelta(days=45),
        status_stages=settings.status_stages(),
    )  # fmt: skip
    print(f"Листы: {', '.join(f'{s.name} ({s.used_weeks}/{s.weeks} нед.)' for s in result.sheets)}")
    for warning in result.warnings:
        print(f"⚠️  {warning}")
    print(f"\nПосты с {start:%d.%m.%Y} по {end:%d.%m.%Y}:")
    print(f"{'дата':<9} {'сл':<3} {'рубрика':<16} {'статус в КП':<24} {'этап':<22} тема / сроки")
    for slot in result.slots:
        if not start <= slot.date <= end:
            continue
        stage = stage_from_kp_status(slot.status, settings.status_stages())
        chain = compute_chain(slot.date, calendar, settings.deadlines, settings.tz)
        topic = slot.topic or "— тема не выбрана —"
        print(
            f"{fmt.wd_date(slot.date):<9} {slot.slot:<3} {(slot.rubric or '—'):<16} "
            f"{(slot.status or '—'):<24} {(LABEL[stage] if stage else '—'):<22} "
            f"{fmt.clip(topic, 60)}{' [док]' if slot.doc_url else ''}"
        )
        print(
            f"{'':<80}взять до {chain.take_day:%d.%m}, текст до {chain.text_day:%d.%m}, "
            f"дизайн и показ {chain.show_day:%d.%m}"
        )
    print(f"\nКомментариев в ячейках календаря: {sum(1 for c in result.comments if c.day)}")
    return 0


def cmd_doctor(env: Env, settings: Settings) -> int:
    from norm_tasker.doctor import run_doctor

    return asyncio.run(run_doctor(env, settings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="norm_tasker", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный журнал")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="запустить бота (по умолчанию)")
    sub.add_parser("doctor", help="проверить настройки, доступы и права бота")
    parse = sub.add_parser("parse", help="показать, как бот разбирает выгрузку КП (xlsx)")
    parse.add_argument("file", type=Path)
    parse.add_argument("--weeks", type=int, default=3, help="сколько недель показать")
    parse.add_argument("--since", type=date.fromisoformat, help="с какой даты, ГГГГ-ММ-ДД")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    env = load_env()
    if args.command == "parse":
        settings = load_settings(env.config_path) if env.config_path.exists() else None
        return cmd_parse(env, settings, args.file, args.weeks, args.since)
    try:
        settings = load_settings(env.config_path)
    except (FileNotFoundError, ValueError) as error:
        print(f"Ошибка настроек: {error}", file=sys.stderr)
        return 2
    if args.command == "doctor":
        return cmd_doctor(env, settings)
    return cmd_run(env, settings)


if __name__ == "__main__":
    raise SystemExit(main())
