from datetime import time
from pathlib import Path

import pytest

from kp_fixture import build_kp, standard_kp
from norm_tasker.__main__ import main
from norm_tasker.config import Role, Settings, load_env, load_settings

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_is_valid_and_matches_defaults():
    settings = load_settings(ROOT / "config.example.yaml")
    defaults = Settings()
    assert settings.deadlines == defaults.deadlines
    assert settings.schedule == defaults.schedule
    assert settings.kp == defaults.kp
    assert [m.role for m in settings.team] == [
        Role.COPYWRITER, Role.COPYWRITER, Role.RESPONSIBLE, Role.BOSS,
    ]  # fmt: skip
    assert settings.deadlines.day_end == time(18, 0)


def test_unquoted_times_do_not_break_the_config(tmp_path):
    """YAML читает 9:30 без кавычек как число 570 — читаем это как минуты от полуночи."""
    path = tmp_path / "config.yaml"
    path.write_text("schedule:\n  summary: 9:30\n  weekly_report: '17:00'\n", encoding="utf-8")
    settings = load_settings(path)
    assert settings.schedule.summary == time(9, 30)
    assert settings.schedule.weekly_report == time(17, 0)


def test_config_typos_and_bad_values_are_explained(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("schedul: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="schedul"):
        load_settings(path)
    path.write_text("timezone: Mars/Base\n", encoding="utf-8")
    with pytest.raises(ValueError, match="часовой пояс"):
        load_settings(path)
    path.write_text("schedule:\n  summary: '25:99'\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(path)
    path.write_text("kp_statuses:\n  Что-то: PUBLISHD\n", encoding="utf-8")
    with pytest.raises(ValueError, match="неизвестный этап"):
        load_settings(path)
    with pytest.raises(FileNotFoundError, match=r"config\.example\.yaml"):
        load_settings(tmp_path / "нет.yaml")


def test_custom_status_mapping_extends_the_defaults():
    settings = Settings(kp_statuses={"Согласовано клиентом": "text_ok"})
    mapping = settings.status_stages()
    assert mapping["согласовано клиентом"].name == "TEXT_OK"
    assert mapping["выпущено"].name == "PUBLISHED"  # стандартное осталось


def test_env_loading(tmp_path):
    env = load_env({"BOT_TOKEN": "1:x", "DATA_DIR": str(tmp_path), "KP_SPREADSHEET_ID": "kp"})
    assert env.bot_token == "1:x" and env.db_path == tmp_path / "tracker.db"
    assert env.config_path == tmp_path / "config.yaml"
    assert env.google_credentials is None and env.tracker_spreadsheet_id is None


def test_parse_command_on_a_kp_file(tmp_path, capsys):
    path = tmp_path / "kp.xlsx"
    path.write_bytes(build_kp(standard_kp()))
    assert main(["parse", str(path), "--since", "2026-09-28", "--weeks", "1"]) == 0
    out = capsys.readouterr().out
    assert "Октябрь 2026" in out and "Новая тема A" in out
    assert "взять до" in out and "дизайн и показ" in out
    assert "Странный статус" in out  # предупреждение о неизвестном статусе


def test_run_without_config_says_what_to_do(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    assert main(["doctor"]) == 2
    assert "config.example.yaml" in capsys.readouterr().err


def test_health_command(tmp_path, capsys, monkeypatch):
    from datetime import datetime, timedelta

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert main(["health"]) == 1  # базы ещё нет
    from norm_tasker.tracker.db import Database
    from norm_tasker.tracker.state import State

    db = Database(tmp_path / "tracker.db")
    state = State(db)
    assert main(["health"]) == 1  # метки работы нет
    state.set_meta("heartbeat", datetime.now().astimezone().isoformat())
    assert main(["health"]) == 0
    state.set_meta("heartbeat", (datetime.now().astimezone() - timedelta(minutes=10)).isoformat())
    assert main(["health"]) == 1
    db.close()


def test_bot_token_never_reaches_the_log():
    import logging

    from norm_tasker.__main__ import RedactingFormatter

    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    record = logging.LogRecord(
        "x",
        logging.ERROR,
        __file__,
        1,
        "GET https://api.telegram.org/bot%s/getMe failed",
        (token,),
        None,
    )
    formatted = RedactingFormatter("%(message)s").format(record)
    assert token not in formatted and "<токен скрыт>" in formatted
