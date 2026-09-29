"""Клиент Google Drive и Sheets на google-auth и requests.

Нужны всего несколько запросов, поэтому тяжёлая клиентская библиотека Google не подключается.
Все вызовы блокирующие — из асинхронного кода их нужно запускать в потоке.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from norm_tasker.tracker.models import ExternalComment

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]
DRIVE = "https://www.googleapis.com/drive/v3"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TIMEOUT = 60

HINTS = {
    401: "ключ сервисного аккаунта не принят: проверьте файл ключа",
    403: "нет прав или не включён API (Drive API / Sheets API в проекте Google Cloud)",
    404: "файл не найден или не расшарен на сервисный аккаунт (нужен доступ читателя)",
}


class GoogleError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Google API {status}: {message}")
        self.status = status


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GoogleClient:
    def __init__(self, session: Any, service_account_email: str = "") -> None:
        self.session = session
        self.service_account_email = service_account_email

    @classmethod
    def from_service_account(cls, path: Path) -> GoogleClient:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        credentials = service_account.Credentials.from_service_account_file(
            str(path), scopes=SCOPES
        )
        session = AuthorizedSession(credentials)
        retry = Retry(
            total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=None,
        )  # fmt: skip
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return cls(session, getattr(credentials, "service_account_email", ""))

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self.session.request(method, url, timeout=TIMEOUT, **kwargs)
        if response.status_code >= 400:
            hint = HINTS.get(response.status_code, "")
            try:
                detail = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                detail = (response.text or "")[:200]
            raise GoogleError(response.status_code, f"{detail}. {hint}".strip())
        return response

    # --- Drive -----------------------------------------------------------------------------
    def file_info(self, file_id: str) -> dict[str, Any]:
        fields = "id,name,mimeType,modifiedTime,version,capabilities/canEdit"
        query = f"fields={quote(fields, safe=',/')}&supportsAllDrives=true"
        url = f"{DRIVE}/files/{quote(file_id)}?{query}"
        return self._request("GET", url).json()

    def export_xlsx(self, file_id: str) -> bytes:
        url = f"{DRIVE}/files/{quote(file_id)}/export?mimeType={quote(XLSX, safe='')}"
        return self._request("GET", url).content

    def doc_comments(self, file_id: str) -> list[ExternalComment]:
        """Комментарии дока (ветки и ответы). Права читателя на док достаточно."""
        fields = (
            "nextPageToken,comments(id,createdTime,resolved,content,author/displayName,"
            "replies(id,createdTime,content,action,author/displayName))"
        )
        result: list[ExternalComment] = []
        page = ""
        while True:
            url = (
                f"{DRIVE}/files/{quote(file_id)}/comments?fields={quote(fields, safe=',/()')}"
                f"&includeDeleted=false&pageSize=100{page}&supportsAllDrives=true"
            )
            data = self._request("GET", url).json()
            for item in data.get("comments", []):
                result.extend(comment_from_drive(file_id, item))
            token = data.get("nextPageToken")
            if not token:
                return result
            page = f"&pageToken={quote(token)}"

    # --- Sheets ----------------------------------------------------------------------------
    def sheet_tabs(self, spreadsheet_id: str) -> dict[str, int]:
        url = f"{SHEETS}/{quote(spreadsheet_id)}?fields=sheets.properties(sheetId,title)"
        data = self._request("GET", url).json()
        return {
            s["properties"]["title"]: s["properties"]["sheetId"] for s in data.get("sheets", [])
        }

    def add_tab(self, spreadsheet_id: str, title: str) -> None:
        url = f"{SHEETS}/{quote(spreadsheet_id)}:batchUpdate"
        body = {"requests": [{"addSheet": {"properties": {"title": title}}}]}
        self._request("POST", url, json=body)

    def replace_values(self, spreadsheet_id: str, tab: str, rows: list[list[str]]) -> None:
        """Записывает таблицу на вкладку с A1, стирая прежнее содержимое."""
        base = f"{SHEETS}/{quote(spreadsheet_id)}/values"
        self._request("POST", f"{base}/{quote(chr(39) + tab + chr(39), safe='')}:clear", json={})
        cell = quote(f"{chr(39)}{tab}{chr(39)}!A1", safe="")
        self._request("PUT", f"{base}/{cell}?valueInputOption=RAW", json={"values": rows})


def comment_from_drive(file_id: str, item: dict[str, Any]) -> list[ExternalComment]:
    """Комментарий Drive → корневой комментарий и его ответы (ответы без текста пропускаются)."""
    resolved = bool(item.get("resolved"))

    def make(cid: str, node: dict[str, Any]) -> ExternalComment:
        author = (node.get("author") or {}).get("displayName") or "неизвестный автор"
        return ExternalComment(
            id=f"doc:{file_id}:{cid}",
            source="doc",
            author=author,
            text=(node.get("content") or "").strip(),
            created=_parse_time(node.get("createdTime")),
            resolved=resolved,
            doc_id=file_id,
        )

    result = []
    if (item.get("content") or "").strip():
        result.append(make(item["id"], item))
    for reply in item.get("replies", []):
        if (reply.get("content") or "").strip():
            result.append(make(f"{item['id']}:{reply['id']}", reply))
    return result
