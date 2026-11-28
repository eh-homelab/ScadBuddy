"""Task widgets (#8062): the screen control a prompt's field asks for, and the blocks of a
task's context. This module does no I/O at import: a workflow imports it."""

from __future__ import annotations

from typing import Any, TypedDict

from scadbuddy.codes.parse import format_code, parse_code
from scadbuddy.codes.resolve import PREFIXES
from scadbuddy.entities.registry import KINDS

WIDGETS = ("scan", "photo", "file", "entity", "weight", "confirm")
#: The keys a property may add to its type's own, for a prompt.
WIDGET_KEYS = frozenset({"x-sb-widget", "expects", "kind"})
#: What an `entity` field may pick.
ENTITY_KINDS = ("library_file", "location", "spool")
MAX_EXPECTS = 20

_TYPES: dict[str, tuple[str, ...]] = {
    "scan": ("string",),
    "photo": ("string",),
    "file": ("string",),
    "entity": ("string",),
    "weight": ("number", "integer"),
    "confirm": ("boolean",),
}
#: The kinds a code's prefix gives (`codes/resolve.py`).
_CODE_KINDS = tuple(lookup.kind for lookup in PREFIXES.values())

MAX_TEXT = 4000
MAX_ROWS = 50
MAX_ITEMS = 30
_BLOCKS = ("entity", "text", "image", "table", "checklist", "expect")


class Expects(TypedDict):
    kinds: tuple[str, ...]
    codes: tuple[str, ...]


def widget_problems(prop: dict[str, Any], type_: str) -> list[str]:
    """What is wrong with the widget keys of one property of JSON type ``type_``."""
    widget = prop.get("x-sb-widget")
    problems: list[str] = []
    if "x-sb-widget" in prop:
        if not isinstance(widget, str):
            problems.append("'x-sb-widget' must be text")
        # An unknown widget is no problem: a form shows the plain type's control for it.
        elif widget in WIDGETS and type_ not in _TYPES[widget]:
            problems.append(f"'x-sb-widget' {widget} needs {' or '.join(_TYPES[widget])}")
    if "kind" in prop:
        if widget != "entity":
            problems.append("'kind' needs 'x-sb-widget' entity")
        elif prop["kind"] not in ENTITY_KINDS:
            problems.append(f"'kind' must be one of {', '.join(ENTITY_KINDS)}")
    elif widget == "entity":
        problems.append("'x-sb-widget' entity needs 'kind'")
    if "expects" in prop:
        if widget != "scan":
            problems.append("'expects' needs 'x-sb-widget' scan")
        else:
            problems += _expects_problems(prop["expects"])
    return problems


def _expects_problems(expects: Any) -> list[str]:
    if not isinstance(expects, dict):
        return ["'expects' must be an object"]
    problems = [
        f"'expects' has {key!r}, which is not supported"
        for key in expects
        if key not in ("kinds", "codes")
    ]
    kinds, codes = expects.get("kinds", []), expects.get("codes", [])
    for name, value in (("kinds", kinds), ("codes", codes)):
        if not isinstance(value, list) or not all(isinstance(one, str) for one in value):
            problems.append(f"'expects' {name} must be a list of text")
        elif len(value) > MAX_EXPECTS:
            problems.append(f"'expects' {name} has at most {MAX_EXPECTS} entries")
    if problems:
        return problems
    if not kinds and not codes:
        return ["'expects' names no kind or code"]
    if any(one not in _CODE_KINDS for one in kinds):
        problems.append(f"'expects' kinds must be from {', '.join(_CODE_KINDS)}")
    if any((code := parse_code(one)) is None or code.prefix not in PREFIXES for one in codes):
        problems.append("'expects' codes must be codes such as L-12 or S-3")
    return problems


def scan_fields(schema: dict[str, Any]) -> dict[str, Expects]:
    """Each scan field of a prompt's schema, with what it expects (empty when it names none)."""
    found: dict[str, Expects] = {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return found
    for name, prop in properties.items():
        if not isinstance(prop, dict) or prop.get("x-sb-widget") != "scan":
            continue
        expects = prop.get("expects")
        expects = expects if isinstance(expects, dict) else {}
        kinds = tuple(k for k in expects.get("kinds", ()) if isinstance(k, str))
        codes = tuple(_code(c) for c in expects.get("codes", ()) if isinstance(c, str))
        found[name] = {"kinds": kinds, "codes": tuple(c for c in codes if c)}
    return found


def _code(text: str) -> str:
    parsed = parse_code(text)
    return format_code(parsed.prefix, parsed.number) if parsed else ""


def expects_entries(schema: dict[str, Any]) -> list[str]:
    """``kind:<kind>`` and ``code:<CODE>`` for every scan field, each once."""
    entries: list[str] = []
    for expects in scan_fields(schema).values():
        for entry in (
            *(f"kind:{kind}" for kind in expects["kinds"]),
            *(f"code:{code}" for code in expects["codes"]),
        ):
            if entry not in entries:
                entries.append(entry)
    return entries


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _entity_ref_ok(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    kind, sep, ref = value.partition(":")
    return bool(sep and ref and kind in KINDS)


def _block_problems(name: str, value: Any) -> str | None:
    if name == "entity":
        return None if _entity_ref_ok(value) else "an entity is '<kind>:<ref>' of a known kind"
    if name == "text":
        if isinstance(value, str) and len(value) <= MAX_TEXT:
            return None
        return f"a text is at most {MAX_TEXT} characters"
    if name == "image":
        ok = (
            isinstance(value, dict)
            and set(value) == {"entity", "attachment_id"}
            and _entity_ref_ok(value["entity"])
            and isinstance(value["attachment_id"], str)
            and bool(value["attachment_id"])
        )
        return None if ok else "an image is {entity, attachment_id}"
    if name == "table":
        return _table_problem(value)
    if name == "checklist":
        ok = (
            isinstance(value, list)
            and len(value) <= MAX_ITEMS
            and all(isinstance(one, str) for one in value)
        )
        return None if ok else f"a checklist is at most {MAX_ITEMS} texts"
    ok = (
        isinstance(value, dict)
        and set(value) == {"weight_g", "tolerance_g"}
        and _number(value["weight_g"])
        and value["weight_g"] > 0
        and _number(value["tolerance_g"])
        and value["tolerance_g"] >= 0
    )
    return None if ok else "an expect is {weight_g above 0, tolerance_g of 0 or more}"


def _table_problem(value: Any) -> str | None:
    if not isinstance(value, dict) or set(value) != {"columns", "rows"}:
        return "a table is {columns, rows}"
    columns, rows = value["columns"], value["rows"]
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        return "a table's columns are texts"
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        return f"a table has at most {MAX_ROWS} rows"
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            return "each row of a table is as wide as its columns"
        if not all(one is None or isinstance(one, str | int | float | bool) for one in row):
            return "a table's cells are text, numbers or booleans"
    return None


def context_problems(context: list[dict[str, Any]]) -> list[str]:
    """What is wrong with the blocks of a task's context. An item that is not a block
    (one key of `_BLOCKS`) is stored as it is, so A1 flows keep working."""
    problems: list[str] = []
    for index, item in enumerate(context):
        if len(item) != 1:
            continue
        ((name, value),) = item.items()
        if name not in _BLOCKS:
            continue
        if (found := _block_problems(name, value)) is not None:
            problems.append(f"context[{index}] {name}: {found}")
    return problems
