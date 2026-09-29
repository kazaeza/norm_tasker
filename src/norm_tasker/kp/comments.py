"""Комментарии из выгрузки КП в xlsx.

Обычный разбор xlsx их не видит: Google кладёт ветки комментариев в отдельные части
файла (xl/threadedComments), и только там есть привязка к ячейке и дата создания.
"""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from xml.etree import ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_REL_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_THREADED = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"
REL_THREADED = "http://schemas.microsoft.com/office/2017/10/relationships/threadedComment"
REL_PERSON = "http://schemas.microsoft.com/office/2017/10/relationships/person"


@dataclass(frozen=True)
class RawComment:
    sheet: str
    cell: str
    id: str
    parent_id: str | None
    author: str
    text: str
    created: datetime | None
    resolved: bool


def _rels(archive: zipfile.ZipFile, path: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(archive.read(path))
    except KeyError:
        return []
    return [dict(rel.attrib) for rel in root.iter(f"{{{NS_REL_PKG}}}Relationship")]


def resolve_part(base_dir: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, target))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z"))
    except ValueError:
        return None


def read_threaded_comments(data: bytes) -> list[RawComment]:
    """Все комментарии всех листов. Пустой список, если в файле их нет."""
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        return []
    with archive:
        try:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        except KeyError:
            return []
        workbook_rels = {r["Id"]: r for r in _rels(archive, "xl/_rels/workbook.xml.rels")}

        persons: dict[str, str] = {}
        for rel in workbook_rels.values():
            if rel.get("Type") == REL_PERSON:
                person_path = resolve_part("xl", rel["Target"])
                try:
                    person_root = ET.fromstring(archive.read(person_path))
                except KeyError:
                    continue
                for person in person_root.iter(f"{{{NS_THREADED}}}person"):
                    persons[person.get("id", "")] = person.get("displayName", "")

        comments: list[RawComment] = []
        for sheet in workbook.iter(f"{{{NS_MAIN}}}sheet"):
            rel = workbook_rels.get(sheet.get(f"{{{NS_REL_DOC}}}id", ""))
            if rel is None:
                continue
            sheet_path = resolve_part("xl", rel["Target"])
            rels_path = posixpath.join(
                posixpath.dirname(sheet_path), "_rels", posixpath.basename(sheet_path) + ".rels"
            )
            for sheet_rel in _rels(archive, rels_path):
                if sheet_rel.get("Type") != REL_THREADED:
                    continue
                comments_path = resolve_part(posixpath.dirname(sheet_path), sheet_rel["Target"])
                try:
                    root = ET.fromstring(archive.read(comments_path))
                except KeyError:
                    continue
                for item in root.iter(f"{{{NS_THREADED}}}threadedComment"):
                    text_node = item.find(f"{{{NS_THREADED}}}text")
                    comments.append(
                        RawComment(
                            sheet=sheet.get("name", ""),
                            cell=item.get("ref", ""),
                            id=item.get("id", ""),
                            parent_id=item.get("parentId"),
                            author=persons.get(item.get("personId", ""), "") or "неизвестный автор",
                            text=(text_node.text or "").strip() if text_node is not None else "",
                            created=_parse_time(item.get("dT")),
                            resolved=item.get("done") == "1",
                        )
                    )
        return comments
