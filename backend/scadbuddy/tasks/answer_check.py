"""What an answer's scan and photo fields name (#8062): the checks that need the database.
`validate.py` has checked the answer against the schema first. A message never quotes the
answer, and never says which of the reasons it is that a code is refused."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.codes.parse import Code, format_code, parse_code
from scadbuddy.codes.resolve import PREFIXES, resolve
from scadbuddy.tasks.models import AnswerProblem
from scadbuddy.tasks.widgets import Expects, scan_fields

_FILE_WIDGETS = ("photo", "file")


def _file_fields(schema: dict[str, Any]) -> list[str]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    return [
        name
        for name, prop in properties.items()
        if isinstance(prop, dict) and prop.get("x-sb-widget") in _FILE_WIDGETS
    ]


def _uuid(value: str) -> str | None:
    """``value`` as the text an attachment's id is stored as, or None if it is no UUID."""
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _accepts(expects: Expects, kind: str, code: Code) -> bool:
    if format_code(code.prefix, code.number) in expects["codes"]:
        return True
    if kind in expects["kinds"]:
        return True
    return not expects["kinds"] and not expects["codes"]


def scan_and_file_problems(
    conn: Connection[DictRow], task_id: str, schema: dict[str, Any], answer: dict[str, Any]
) -> list[AnswerProblem]:
    """The scan fields whose code is not one the question takes (it parses, is of a kind
    or is a code the field expects, and names a row that is not archived), and the photo
    and file fields that are not an attachment of this task."""
    problems: list[AnswerProblem] = []
    for name, expects in scan_fields(schema).items():
        value = answer.get(name)
        if not isinstance(value, str):
            continue
        code = parse_code(value)
        lookup = PREFIXES.get(code.prefix) if code else None
        if code is None or lookup is None or not _accepts(expects, lookup.kind, code):
            problems.append(AnswerProblem(field=name, message="is not a code this question takes"))
            continue
        found = resolve(conn, code)
        if found is None or found.archived:
            problems.append(AnswerProblem(field=name, message="names nothing that is in use"))
    for name in _file_fields(schema):
        value = answer.get(name)
        if not isinstance(value, str):
            continue
        attachment = _uuid(value)
        held = (
            None
            if attachment is None
            else conn.execute(
                "SELECT 1 FROM entity_attachments WHERE subject = %s AND id = %s",
                (f"task:{task_id}", attachment),
            ).fetchone()
        )
        if held is None:
            problems.append(AnswerProblem(field=name, message="is not a file of this task"))
    return problems
