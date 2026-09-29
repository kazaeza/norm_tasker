"""Подставной Telegram: апдейты идут через настоящие обработчики бота, а вместо сети —
запись вызовов API и заготовленные ответы."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from norm_tasker.tg.api import Api
from norm_tasker.tg.errors import BadRequest
from norm_tasker.tg.types import CallbackQuery, Chat, Message, Update, User

BOT_ID = 42
WORK_CHAT = -1001234567890


class FakeApi(Api):
    """Вызовы Bot API записываются как («SendMessage», {параметры}) и получают готовый ответ."""

    def __init__(self) -> None:
        super().__init__("42:TEST", transport=object())  # type: ignore[arg-type]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 1000
        self.reads_all = True
        self.can_pin = True
        self.reject_reactions = False
        self.reject_pin = False
        self.failures: dict[str, list[Exception]] = {}

    def fail_next(self, method: str, *errors: Exception) -> None:
        """Следующие вызовы метода закончатся этими ошибками (по одной на вызов)."""
        self.failures.setdefault(method, []).extend(errors)

    async def close(self) -> None:
        return None

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30
    ) -> Any:
        payload = {key: value for key, value in (params or {}).items() if value is not None}
        name = method[0].upper() + method[1:]
        self.calls.append((name, payload))
        if self.failures.get(method):
            raise self.failures[method].pop(0)
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return True
        result = handler(payload)
        return await result if asyncio.iscoroutine(result) else result

    # --- ответы на вызовы ---------------------------------------------------------------
    async def _GetUpdates(self, payload: dict[str, Any]) -> list:
        await asyncio.sleep(0.05)  # настоящий long polling ждёт; не крутим цикл вхолостую
        return []

    def _GetMe(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": BOT_ID, "is_bot": True, "first_name": "Тест", "username": "norm_test_bot",
            "can_read_all_group_messages": self.reads_all,
        }  # fmt: skip

    def _SendMessage(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        return {
            "message_id": self._next_id,
            "date": 0,
            "chat": {"id": payload["chat_id"], "type": "supergroup"},
            "text": payload["text"],
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Тест"},
        }

    def _GetChatMember(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "administrator",
            "user": {"id": BOT_ID, "is_bot": True, "first_name": "Тест"},
            "can_pin_messages": self.can_pin,
        }

    def _SetMessageReaction(self, payload: dict[str, Any]) -> bool:
        if self.reject_reactions:
            raise BadRequest("Bad Request: REACTION_INVALID", method="setMessageReaction", code=400)
        return True

    def _PinChatMessage(self, payload: dict[str, Any]) -> bool:
        if self.reject_pin:
            raise BadRequest(
                "Bad Request: not enough rights to pin a message",
                method="pinChatMessage",
                code=400,
            )
        return True

    # --- удобные выборки ---------------------------------------------------------------------
    def named(self, name: str) -> list[dict[str, Any]]:
        return [data for call, data in self.calls if call == name]

    def sent_texts(self) -> list[str]:
        return [data["text"] for data in self.named("SendMessage")]

    def last_text(self) -> str:
        return self.sent_texts()[-1]

    def clear(self) -> None:
        self.calls.clear()


def make_user(user_id: int, username: str | None, name: str) -> User:
    return User(id=user_id, is_bot=False, first_name=name, username=username)


_counter = {"update": 0, "message": 5000}


def _next(kind: str) -> int:
    _counter[kind] += 1
    return _counter[kind]


def text_update(
    text: str,
    *,
    user_id: int,
    username: str | None,
    name: str = "Участник",
    chat_id: int = WORK_CHAT,
    reply_to: Message | None = None,
    chat_type: str = "supergroup",
) -> Update:
    message = Message(
        message_id=_next("message"),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=make_user(user_id, username, name),
        text=text,
        reply_to_message=reply_to,
    )
    return Update(update_id=_next("update"), message=message)


def bot_message(message_id: int, chat_id: int = WORK_CHAT) -> Message:
    """Сообщение, которое бот отправил раньше: на него можно ответить."""
    return Message(
        message_id=message_id,
        chat=Chat(id=chat_id, type="supergroup"),
        from_user=User(id=BOT_ID, is_bot=True, first_name="Тест"),
        text="сообщение бота",
    )


def callback_update(
    data: str,
    *,
    user_id: int,
    username: str | None,
    message_id: int,
    name: str = "Участник",
    chat_id: int = WORK_CHAT,
) -> Update:
    query = CallbackQuery(
        id=str(_next("update")),
        from_user=make_user(user_id, username, name),
        message=bot_message(message_id, chat_id),
        data=data,
    )
    return Update(update_id=_next("update"), callback_query=query)


class FakeGoogle:
    """Google без сети: отдаёт заготовленную выгрузку КП и комментарии доков."""

    service_account_email = "bot@example.iam.gserviceaccount.com"

    def __init__(self, xlsx: bytes = b"", doc_comments: dict[str, list] | None = None) -> None:
        self.xlsx = xlsx
        self.docs = doc_comments or {}
        self.written: list[list[list[str]]] = []
        self.tabs: dict[str, int] = {}

    def export_xlsx(self, file_id: str) -> bytes:
        return self.xlsx

    def doc_comments(self, file_id: str) -> list:
        return list(self.docs.get(file_id, []))

    def sheet_tabs(self, spreadsheet_id: str) -> dict[str, int]:
        return dict(self.tabs)

    def add_tab(self, spreadsheet_id: str, title: str) -> None:
        self.tabs[title] = len(self.tabs)

    def replace_values(self, spreadsheet_id: str, tab: str, rows: list[list[str]]) -> None:
        self.written.append(rows)


class FakeTelegram:
    """Подставной сервер Bot API на localhost: те же адреса и формат ответов, что у Telegram.

    Ответы можно заготовить (reply, fail, raw — по очереди на каждый метод). Без заготовки
    сервер отвечает как настоящий: getMe, getChatMember, sendMessage, а getUpdates отдаёт
    очередь пришедших сообщений (push_update) с позиции offset и ждёт, как long polling.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.script: dict[str, list[tuple[int, Any, float]]] = {}
        self.updates: list[dict[str, Any]] = []
        self._message_id = 9000
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                method = self.path.rpartition("/")[2]
                fake.requests.append(
                    {
                        "path": self.path,
                        "method": method,
                        "body": body,
                        "content_type": self.headers.get("Content-Type"),
                    }
                )
                status, payload, pause = fake.next_reply(method, body)
                if pause:
                    time.sleep(pause)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                return None

        class Server(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                return None  # клиент оборвал долгий опрос при остановке — это нормально

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    # --- что ответит сервер --------------------------------------------------------------
    def reply(self, method: str, result: Any = True, *, status: int = 200, pause: float = 0):
        self.script.setdefault(method, []).append((status, {"ok": True, "result": result}, pause))

    def fail(self, method: str, status: int, description: str, **parameters: Any) -> None:
        body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
        if parameters:
            body["parameters"] = parameters
        self.script.setdefault(method, []).append((status, body, 0))

    def raw(self, method: str, status: int, body: bytes) -> None:
        self.script.setdefault(method, []).append((status, body, 0))

    def push_update(self, update: dict[str, Any]) -> None:
        self.updates.append(update)

    def next_reply(self, method: str, body: dict[str, Any]) -> tuple[int, Any, float]:
        queue = self.script.get(method)
        if queue:
            return queue.pop(0)
        return 200, {"ok": True, "result": self.default_result(method, body)}, 0

    def default_result(self, method: str, body: dict[str, Any]) -> Any:
        bot = {"id": BOT_ID, "is_bot": True, "first_name": "Тест", "username": "norm_test_bot"}
        if method == "getMe":
            return {**bot, "can_read_all_group_messages": True}
        if method == "getChatMember":
            return {"status": "administrator", "user": bot, "can_pin_messages": True}
        if method == "sendMessage":
            self._message_id += 1
            chat = {"id": body["chat_id"], "type": "supergroup"}
            return {
                "message_id": self._message_id, "date": 0, "chat": chat, "text": body["text"],
                "from": bot,
            }  # fmt: skip
        if method == "getUpdates":
            found = [u for u in self.updates if u["update_id"] >= (body.get("offset") or 0)]
            if not found:
                time.sleep(min(body.get("timeout") or 0, 0.2))  # long polling, но недолго
            return found
        return True

    # --- что видел сервер ----------------------------------------------------------------------
    def bodies(self, method: str) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests if r["method"] == method]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def message_json(
    update_id: int, text: str, *, user_id: int = 101, username: str = "cw_alpha"
) -> dict[str, Any]:
    """Сообщение рабочего чата в том виде, в каком его присылает Telegram."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1780000000,
            "chat": {"id": WORK_CHAT, "type": "supergroup", "title": "Команда"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Альфа", "username": username},
            "text": text,
        },
    }
