"""Производственный календарь РФ и арифметика рабочих дней.

Данные берутся из isdayoff.ru (строка на год: по символу на день; 0 — рабочий,
1 — нерабочий, 2 — сокращённый). Встроенная копия лежит в data/holidays_ru.json и
нужна, пока сервер не достучался до источника. Поверх всего действуют ручные
исключения из настроек.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import date, timedelta
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

ISDAYOFF_URL = "https://isdayoff.ru/api/getdata?year={year}&pre=1"


def days_in_year(year: int) -> int:
    return 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365


def is_valid_year_string(year: int, data: str) -> bool:
    """Строка годится, если в ней ровно по символу на день и есть нормальное число выходных.

    На будущий год, для которого календарь ещё не утверждён, источник отдаёт одни нули —
    такие данные хуже, чем «пн–пт», поэтому отбрасываем.
    """
    return (
        len(data) == days_in_year(year)
        and set(data) <= set("0124")
        and data.count("1") >= 100  # 52 недели по 2 выходных — минимум
    )


class WorkCalendar:
    """Рабочие дни. Если данных на год нет, рабочими считаются понедельник–пятница."""

    def __init__(
        self,
        year_data: dict[int, str] | None = None,
        extra_days_off: Iterable[date] = (),
        extra_workdays: Iterable[date] = (),
    ) -> None:
        self._years: dict[int, str] = {}
        for year, data in (year_data or {}).items():
            self.set_year(year, data)
        self._extra_days_off = set(extra_days_off)
        self._extra_workdays = set(extra_workdays)

    def set_year(self, year: int, data: str) -> bool:
        if not is_valid_year_string(year, data):
            log.warning("Календарь на %s не принят: неверный формат данных", year)
            return False
        self._years[year] = data
        return True

    def has_year(self, year: int) -> bool:
        return year in self._years

    def is_workday(self, day: date) -> bool:
        if day in self._extra_workdays:
            return True
        if day in self._extra_days_off:
            return False
        data = self._years.get(day.year)
        if data is not None:
            return data[day.timetuple().tm_yday - 1] != "1"
        return day.weekday() < 5

    def is_short_day(self, day: date) -> bool:
        data = self._years.get(day.year)
        return bool(data) and data[day.timetuple().tm_yday - 1] == "2"

    def shift_workdays(self, day: date, n: int) -> date:
        """Сдвигает дату на n рабочих дней (n < 0 — назад). Сама дата может быть выходным.

        D−1 для поста на субботу — пятница, а для поста на понедельник — тоже пятница.
        """
        step = timedelta(days=1 if n > 0 else -1)
        for _ in range(abs(n)):
            day += step
            while not self.is_workday(day):
                day += step
        return day

    def prev_workday(self, day: date, n: int = 1) -> date:
        return self.shift_workdays(day, -n)

    def next_workday(self, day: date, n: int = 1) -> date:
        return self.shift_workdays(day, n)

    def on_or_after(self, day: date) -> date:
        """Сама дата, если она рабочая, иначе ближайший следующий рабочий день."""
        while not self.is_workday(day):
            day += timedelta(days=1)
        return day

    def workdays_between(self, start: date, end: date) -> int:
        """Сколько рабочих дней в промежутке [start, end] включительно."""
        count = 0
        day = start
        while day <= end:
            count += self.is_workday(day)
            day += timedelta(days=1)
        return count


def load_bundled() -> dict[int, str]:
    text = resources.files("norm_tasker").joinpath("data/holidays_ru.json").read_text("utf-8")
    return {int(k): v for k, v in json.loads(text).items() if k.isdigit()}


def load_cache(path: Path) -> dict[int, str]:
    try:
        raw = json.loads(path.read_text("utf-8"))
        return {int(k): v for k, v in raw.items() if k.isdigit()}
    except (OSError, ValueError):
        return {}


def save_cache(path: Path, data: dict[int, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({str(k): v for k, v in data.items()}), "utf-8")
    except OSError:
        log.warning("Не удалось сохранить кэш календаря в %s", path, exc_info=True)


def fetch_year(year: int, timeout: float = 15.0) -> str | None:
    """Скачивает год с isdayoff.ru. Возвращает None, если данных нет или сеть недоступна."""
    import requests  # ленивый импорт: остальной модуль работает и без сети

    try:
        response = requests.get(ISDAYOFF_URL.format(year=year), timeout=timeout)
        response.raise_for_status()
    except requests.RequestException:
        log.warning("Не удалось получить календарь на %s с isdayoff.ru", year, exc_info=True)
        return None
    data = response.text.strip()
    return data if is_valid_year_string(year, data) else None


def build_calendar(
    cache_path: Path | None = None,
    extra_days_off: Iterable[date] = (),
    extra_workdays: Iterable[date] = (),
) -> WorkCalendar:
    """Встроенные данные, поверх них кэш с прошлых запусков."""
    years = load_bundled()
    if cache_path is not None:
        years.update(load_cache(cache_path))
    return WorkCalendar(years, extra_days_off, extra_workdays)


def refresh_calendar(calendar: WorkCalendar, years: Iterable[int], cache_path: Path | None) -> None:
    """Обновляет данные из сети (блокирующий вызов — запускать в потоке)."""
    fresh: dict[int, str] = {}
    for year in years:
        data = fetch_year(year)
        if data is not None and calendar.set_year(year, data):
            fresh[year] = data
    if fresh and cache_path is not None:
        merged = load_cache(cache_path)
        merged.update(fresh)
        save_cache(cache_path, merged)
