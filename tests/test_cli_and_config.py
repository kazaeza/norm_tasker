from datetime import time
from pathlib import Path

import pytest

from kp_fixture import build_kp, standard_kp
from norm_tasker.__main__ import main
from norm_tasker.config import Role, Settings, load_env, load_settings, load_settings_for

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


YAML_TEXT = "chat_id: -1001\nteam:\n  - {name: Первый, username: first, role: copywriter}\n"


def test_soft_start_can_be_switched_off_by_a_variable(tmp_path):
    """Напоминания включаются одной переменной, без правки закодированных настроек."""
    text = YAML_TEXT + "soft_start_days: 14\n"
    base = {"DATA_DIR": str(tmp_path), "CONFIG_YAML": text}
    assert load_settings_for(load_env(base)).soft_start_days == 14  # как записано в настройках
    env = load_env({**base, "SOFT_START_DAYS": "0"})
    assert env.soft_start_days == 0 and load_settings_for(env).soft_start_days == 0
    assert load_settings_for(load_env({**base, "SOFT_START_DAYS": "3"})).soft_start_days == 3
    assert load_settings_for(load_env({**base, "SOFT_START_DAYS": "-5"})).soft_start_days == 0
    assert load_settings_for(load_env({**base, "SOFT_START_DAYS": " "})).soft_start_days == 14
    with pytest.raises(ValueError, match="SOFT_START_DAYS"):
        load_env({**base, "SOFT_START_DAYS": "скоро"})


def test_settings_can_come_from_the_environment_instead_of_a_file(tmp_path):
    """В облачных хостингах без файлов настройки лежат в переменной CONFIG_YAML."""
    env = load_env({"DATA_DIR": str(tmp_path), "CONFIG_YAML": YAML_TEXT})
    assert env.has_config
    settings = load_settings_for(env)
    assert settings.chat_id == -1001 and settings.team[0].username == "first"


def test_settings_from_base64_survive_line_breaks(tmp_path):
    import base64

    encoded = base64.b64encode(YAML_TEXT.encode("utf-8")).decode()
    wrapped = encoded[:20] + "\n " + encoded[20:]  # при копировании строку легко разорвать
    env = load_env({"DATA_DIR": str(tmp_path), "CONFIG_B64": wrapped})
    assert load_settings_for(env).chat_id == -1001
    with pytest.raises(ValueError, match="CONFIG_B64"):
        load_env({"CONFIG_B64": "это не base64!"})


def test_chat_id_variables_override_the_settings(tmp_path):
    env = load_env(
        {"DATA_DIR": str(tmp_path), "CONFIG_YAML": YAML_TEXT, "CHAT_ID": "-1009", "THREAD_ID": "7"}
    )
    settings = load_settings_for(env)
    assert settings.chat_id == -1009 and settings.thread_id == 7
    with pytest.raises(ValueError, match="CHAT_ID"):
        load_env({"CHAT_ID": "чат"})


def test_settings_variable_wins_over_the_file_and_file_still_works(tmp_path):
    (tmp_path / "config.yaml").write_text("chat_id: -5\n", encoding="utf-8")
    only_file = load_env({"DATA_DIR": str(tmp_path)})
    assert load_settings_for(only_file).chat_id == -5
    both = load_env({"DATA_DIR": str(tmp_path), "CONFIG_YAML": YAML_TEXT})
    assert load_settings_for(both).chat_id == -1001
    empty = load_env({"DATA_DIR": str(tmp_path / "нет")})
    assert not empty.has_config
    with pytest.raises(FileNotFoundError, match="CONFIG_YAML"):
        load_settings_for(empty)


def test_broken_settings_text_is_explained_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("CONFIG_YAML", "team: [unclosed")
    assert main(["doctor"]) == 2
    assert "Ошибка настроек" in capsys.readouterr().err
    monkeypatch.setenv("CONFIG_YAML", "- просто\n- список\n")
    assert main(["doctor"]) == 2
    assert "ключ: значение" in capsys.readouterr().err


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


def test_gemini_settings_come_from_the_environment():
    env = load_env({"GEMINI_API": " key123456 "})
    assert env.gemini_key == "key123456" and env.ai_on
    assert env.gemini_model is None and env.gemini_url is None
    assert load_env({"GEMINI_API_KEY": "abc"}).gemini_key == "abc"
    assert load_env({"GEMINI_API": "abc", "GEMINI_API_KEY": "zzz"}).gemini_key == "abc"
    custom = load_env(
        {"GEMINI_API": "a", "GEMINI_MODEL": "gemini-x", "GEMINI_URL": "https://p.example"}
    )
    assert custom.gemini_model == "gemini-x" and custom.gemini_url == "https://p.example"
    for off in ("0", "false", "Нет", "OFF"):
        assert not load_env({"GEMINI_API": "abc", "AI_ENABLED": off}).ai_on
    assert not load_env({}).ai_on and not load_env({"GEMINI_API": "   "}).ai_on


def test_gemini_keys_are_hidden_in_the_log():
    import logging

    from norm_tasker.__main__ import SECRETS, RedactingFormatter, hide_secret

    google = "AIza_another_fake_key_00000000"
    other = "proxy-secret-value-123"
    hide_secret(other)
    try:
        record = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "ключ %s и %s", (google, other), None
        )
        formatted = RedactingFormatter("%(message)s").format(record)
        assert google not in formatted and other not in formatted
        assert formatted.count("<ключ скрыт>") == 2
    finally:
        SECRETS.discard(other)
