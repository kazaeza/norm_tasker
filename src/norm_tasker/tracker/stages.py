"""Этапы поста и соответствие статусов КП этапам."""

from __future__ import annotations

from enum import IntEnum


class Stage(IntEnum):
    """Этапы по порядку. Этап поста — самый поздний из известных; назад — только явно."""

    NEW = 0  # пост есть в КП, но о нём ещё ничего не известно
    TAKEN = 1  # взят: пишется текст
    TEXT_SHOWN = 2  # текст показан клиенту
    TEXT_OK = 3  # клиент одобрил текст
    AT_DESIGNER = 4  # док передан дизайнеру
    DESIGN_READY = 5  # дизайн готов
    SHOWN_DESIGN = 6  # готовый пост показан клиенту
    FINAL_OK = 7  # финальный ок клиента
    PUBLISHED = 8  # опубликован или стоит в отложенных


LABEL: dict[Stage, str] = {
    Stage.NEW: "не взят",
    Stage.TAKEN: "взят, пишется текст",
    Stage.TEXT_SHOWN: "текст у клиента",
    Stage.TEXT_OK: "текст одобрен",
    Stage.AT_DESIGNER: "у дизайнера",
    Stage.DESIGN_READY: "дизайн готов",
    Stage.SHOWN_DESIGN: "пост у клиента",
    Stage.FINAL_OK: "финальный ок",
    Stage.PUBLISHED: "опубликован",
}

EMOJI: dict[Stage, str] = {
    Stage.NEW: "⚪️",
    Stage.TAKEN: "✍️",
    Stage.TEXT_SHOWN: "📤",
    Stage.TEXT_OK: "👌",
    Stage.AT_DESIGNER: "🎨",
    Stage.DESIGN_READY: "🖼",
    Stage.SHOWN_DESIGN: "📨",
    Stage.FINAL_OK: "✅",
    Stage.PUBLISHED: "🚀",
}

# Что нужно сделать, чтобы пост дошёл до следующего этапа.
NEXT_STEP: dict[Stage, str] = {
    Stage.NEW: "взять пост",
    Stage.TAKEN: "написать текст и показать клиенту",
    Stage.TEXT_SHOWN: "получить ок клиента по тексту",
    Stage.TEXT_OK: "передать док дизайнеру",
    Stage.AT_DESIGNER: "получить дизайн",
    Stage.DESIGN_READY: "показать клиенту с дизайном",
    Stage.SHOWN_DESIGN: "получить финальный ок",
    Stage.FINAL_OK: "опубликовать или поставить в отложенные",
    Stage.PUBLISHED: "",
}


def normalize(text: str | None) -> str:
    return " ".join((text or "").replace("ё", "е").replace("Ё", "Е").casefold().split())


# Статусы из выпадающего списка КП. Это предположение команды; правится в config.yaml.
DEFAULT_KP_STATUS_STAGES: dict[str, Stage] = {
    "в работе": Stage.TAKEN,
    "текст на согласовании": Stage.TEXT_SHOWN,
    "текст готов": Stage.TEXT_OK,
    "дизайн на согласовании": Stage.SHOWN_DESIGN,
    "готово": Stage.FINAL_OK,
    "выпущено": Stage.PUBLISHED,
}


def stage_from_kp_status(
    status: str | None, mapping: dict[str, Stage] | None = None
) -> Stage | None:
    """Этап по статусу из КП; None, если статус пустой или неизвестен."""
    key = normalize(status)
    if not key:
        return None
    return (mapping or DEFAULT_KP_STATUS_STAGES).get(key)
