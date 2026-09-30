"""Подставной Gemini: ответы заготавливаются по очереди, все запросы записываются."""

from __future__ import annotations

import json

KEY = "AIza_test_key_not_real_0123456"


def answer(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}


def verdict(**fields) -> tuple[int, dict]:
    """Ответ Gemini в формате бота: JSON с решением (kind, text, confidence, actions)."""
    return 200, answer(json.dumps(fields, ensure_ascii=False))


def error_body(status: str, message: str, code: int = 400) -> dict:
    return {"error": {"code": code, "status": status, "message": message}}


class FakeTransport:
    """Отвечает заготовленными (статус, тело) по очереди; исключение из очереди выбрасывается."""

    def __init__(self, *replies: tuple[int, object] | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def post(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def prompt(self, index: int = -1) -> str:
        """Текст запроса, ушедший в Gemini."""
        return self.calls[index]["payload"]["contents"][0]["parts"][0]["text"]
