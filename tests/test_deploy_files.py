"""Файлы запуска в облаке не должны разъехаться с Dockerfile."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text("utf-8")


def amvera() -> dict:
    return yaml.safe_load((ROOT / "amvera.yaml").read_text("utf-8"))


def test_amvera_builds_the_dockerfile():
    config = amvera()
    assert config["meta"] == {"environment": "docker", "toolchain": {"name": "docker"}}
    assert (ROOT / config["build"]["dockerfile"]).is_file()


def test_amvera_keeps_the_folder_where_the_image_writes_the_database():
    data_dir = re.search(r"^\s*(?:ENV\s+)?DATA_DIR=([^\s\\]+)", DOCKERFILE, re.M)
    assert data_dir, "в Dockerfile не задан DATA_DIR"
    # Расхождение тихо унесло бы базу в папку, которая стирается при каждом обновлении.
    assert amvera()["run"]["persistenceMount"] == data_dir.group(1)


def test_amvera_starts_the_bot_with_the_dockerfile_command():
    run = amvera()["run"]
    assert "command" not in run and "args" not in run
    assert 'CMD ["python", "-m", "norm_tasker", "run"]' in DOCKERFILE


def test_dockerfile_has_no_volume_line():
    """Часть хостингов отклоняет сборку со строкой VOLUME."""
    assert not re.search(r"^\s*VOLUME\b", DOCKERFILE, re.M)
