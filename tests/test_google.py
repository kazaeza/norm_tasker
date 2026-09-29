import pytest

from conftest import FRI
from norm_tasker.google.client import GoogleClient, GoogleError, comment_from_drive
from norm_tasker.google.tracker_sheet import HEADER, TAB, rows_for, write_tracker_copy
from norm_tasker.tracker.stages import Stage


class FakeResponse:
    def __init__(self, status=200, data=None, content=b"", text=""):
        self.status_code = status
        self._data = data
        self.content = content
        self.text = text

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeSession:
    """Подставной сеанс: отвечает по подстроке адреса и запоминает все обращения."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, timeout=None, json=None):
        self.calls.append((method, url, json))
        for needle, response in self.routes:
            if needle in url:
                return response() if callable(response) else response
        raise AssertionError(f"нет ответа для {method} {url}")


def client_with(*routes):
    session = FakeSession(list(routes))
    return GoogleClient(session), session


def test_export_downloads_xlsx():
    client, session = client_with(("/export?", FakeResponse(content=b"PK-data")))
    assert client.export_xlsx("file123") == b"PK-data"
    method, url, _ = session.calls[0]
    assert method == "GET" and "/files/file123/export" in url
    assert "spreadsheetml.sheet" in url


@pytest.mark.parametrize(
    ("status", "hint"),
    [(404, "расшарен"), (403, "не включён API"), (401, "ключ сервисного аккаунта")],
)
def test_errors_carry_a_hint(status, hint):
    body = {"error": {"message": "Ошибка от Google"}}
    client, _ = client_with(("/files/", FakeResponse(status, body)))
    with pytest.raises(GoogleError) as info:
        client.file_info("f")
    assert info.value.status == status
    assert "Ошибка от Google" in str(info.value) and hint in str(info.value)


def test_error_without_json_body():
    client, _ = client_with(("/files/", FakeResponse(500, None, text="Internal boom")))
    with pytest.raises(GoogleError, match="Internal boom"):
        client.file_info("f")


def drive_comment(cid, content, *, resolved=False, author="Клиент Один", replies=()):
    return {
        "id": cid,
        "content": content,
        "resolved": resolved,
        "createdTime": "2026-09-28T10:15:00.000Z",
        "author": {"displayName": author},
        "replies": list(replies),
    }


def test_doc_comments_are_paginated_and_flattened():
    page1 = {
        "comments": [
            drive_comment(
                "c1",
                "поменяйте вторую фразу",
                replies=[
                    {"id": "r1", "content": "сделаю", "author": {"displayName": "Копирайтер"}},
                    {"id": "r2", "content": "", "action": "resolve", "author": {}},
                ],
            )
        ],
        "nextPageToken": "TOKEN2",
    }
    page2 = {"comments": [drive_comment("c2", "ок", resolved=True, author="Клиент Два")]}
    client, session = client_with(
        ("pageToken=TOKEN2", FakeResponse(data=page2)), ("/comments?", FakeResponse(data=page1))
    )
    comments = client.doc_comments("docQ")
    assert [c.id for c in comments] == ["doc:docQ:c1", "doc:docQ:c1:r1", "doc:docQ:c2"]
    assert comments[0].author == "Клиент Один" and comments[0].text == "поменяйте вторую фразу"
    assert comments[0].created.year == 2026 and comments[0].doc_id == "docQ"
    assert comments[1].author == "Копирайтер" and not comments[1].resolved
    assert comments[2].resolved  # ветка закрыта
    assert len(session.calls) == 2


def test_comment_without_author_or_time():
    (comment,) = comment_from_drive("d", {"id": "x", "content": "текст"})
    assert comment.author == "неизвестный автор" and comment.created is None


def test_tracker_copy_creates_tab_and_replaces_values(tracker, board, alpha):
    tracker.claim(board[FRI].id, alpha)
    tracker.set_stage(board[FRI].id, Stage.TEXT_SHOWN, alpha)
    client, session = client_with(
        (
            "fields=sheets.properties",
            FakeResponse(data={"sheets": [{"properties": {"sheetId": 0, "title": "Лист1"}}]}),
        ),
        (":batchUpdate", FakeResponse(data={})),
        (":clear", FakeResponse(data={})),
        ("valueInputOption=RAW", FakeResponse(data={})),
    )
    count = write_tracker_copy(client, "SHEET", tracker, tracker.now())
    assert count == 6  # пять постов недели и двух недель вперёд + вышедший вчера
    methods = [(m, u.split("/")[-1][:24]) for m, u, _ in session.calls]
    assert methods[1][0] == "POST" and "batchUpdate" in session.calls[1][1]
    assert session.calls[1][2]["requests"][0]["addSheet"]["properties"]["title"] == TAB
    assert ":clear" in session.calls[2][1]
    written = session.calls[3][2]["values"]
    assert written[0] == HEADER and len(written) == 7
    row = next(r for r in written if r[0] == str(board[FRI].id))
    assert row[6] == "Копирайтер Альфа" and row[7] == "текст у клиента"
    assert row[8] == "получить ок клиента по тексту" and row[9] == "чт 01.10 13:00"


def test_tracker_copy_keeps_existing_tab(tracker, board):
    client, session = client_with(
        (
            "fields=sheets.properties",
            FakeResponse(data={"sheets": [{"properties": {"sheetId": 7, "title": TAB}}]}),
        ),
        (":clear", FakeResponse(data={})),
        ("valueInputOption=RAW", FakeResponse(data={})),
    )
    write_tracker_copy(client, "SHEET", tracker, tracker.now())
    assert not any("batchUpdate" in url for _, url, _ in session.calls)


def test_rows_mark_posts_missing_from_kp(tracker, board):
    tracker._apply(
        board[FRI].id,
        {"in_kp": False, "kp_status": None},
        kind="kp_gone",
        actor=None,
        source="kp",
        undoable=False,
    )
    row = next(r for r in rows_for(tracker, tracker.now()) if r[0] == str(board[FRI].id))
    assert row[10] == "нет в КП"


def test_public_sheet_export_needs_no_key():
    from norm_tasker.google.client import PublicSheet

    class Session:
        def __init__(self, status, ctype):
            self.status, self.ctype = status, ctype

        def get(self, url, timeout=None, allow_redirects=True):
            self.url = url
            return type(
                "R",
                (),
                {
                    "status_code": self.status,
                    "headers": {"content-type": self.ctype},
                    "content": b"xlsx",
                },
            )()

    ok = Session(200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert PublicSheet(ok).export_xlsx("ID1") == b"xlsx" and ok.url.endswith(
        "/d/ID1/export?format=xlsx"
    )
    # Закрытая таблица отдаёт страницу входа (html) — объясняем, что делать.
    with pytest.raises(GoogleError, match="Все, у кого есть ссылка"):
        PublicSheet(Session(200, "text/html")).export_xlsx("ID1")
