"""The outputs list's query (#4777, order operations spec 2026-11-07 §3.1) on
``outputs`` (`OutputRows.query`, #7257, spec §4.5): the filters, the order, the pages
and the parameter text that ``q`` matches.

It imports `outputs.py` only inside a function, because `outputs.py` imports
`output_rows.py`, which imports this module.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow

#: A page's size when the caller names none: the list spans every model.
DEFAULT_LIMIT = 50


class AfterNotListedError(LookupError):
    """``after`` names no output the query lists."""


@dataclass(frozen=True)
class OutputQuery:
    #: ``created_at >= since`` and ``< until``.
    since: datetime | None = None
    until: datetime | None = None
    #: Only these models; ``None`` is every model, an empty list none.
    slugs: Sequence[str] | None = None
    #: Matched in the output's name or its parameter text, case folded.
    q: str | None = None
    #: Whether any order item names the output; ``None`` either.
    ordered: bool | None = None
    #: The storefront slice run's outputs too (#6371); without, the back office's only.
    include_storefront: bool = False
    after: str | None = None
    limit: int = DEFAULT_LIMIT


def _word(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return json.dumps(value)


def params_text(params: Mapping[str, Any], defaults: Mapping[str, Any]) -> str:
    """The parameters that differ from ``defaults`` as ``name=value`` words, by name.
    With no defaults known (no cached schema), every parameter."""
    return " ".join(
        f"{name}={_word(value)}"
        for name, value in sorted(params.items())
        if name not in defaults or defaults[name] != value
    )


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _filters(query: OutputQuery) -> tuple[str, list[Any]]:
    from scadbuddy.library.outputs import BACK_OFFICE

    where: list[str] = []
    args: list[Any] = []
    if query.since is not None:
        where.append("x.created_at >= %s")
        args.append(query.since)
    if query.until is not None:
        where.append("x.created_at < %s")
        args.append(query.until)
    if query.slugs is not None:
        where.append("x.slug = ANY(%s)")
        args.append(list(query.slugs))
    if query.q:
        # An `outputs` row a phase 2 pod saved has no text yet (#7257): it matches no text.
        where.append("(x.name ILIKE %s ESCAPE '\\' OR x.params_text ILIKE %s ESCAPE '\\')")
        args += [_like(query.q), _like(query.q)]
    if not query.include_storefront:
        where.append("x.source = %s")
        args.append(BACK_OFFICE)
    if query.ordered is not None:
        exists = "EXISTS (SELECT 1 FROM order_items i WHERE i.output_id = x.output_id)"
        where.append(exists if query.ordered else f"NOT {exists}")
    return (" WHERE " + " AND ".join(where)) if where else "", args


def query_ids(conn: Connection[DictRow], query: OutputQuery) -> tuple[list[str], int]:
    """The page's output ids from ``outputs``, newest first, and how many the filters
    select. ``after`` must be an id the filters select, else `AfterNotListedError`."""
    where, args = _filters(query)
    counted = conn.execute(f"SELECT count(*) AS n FROM outputs x{where}", args).fetchone()
    assert counted is not None
    total: int = counted["n"]
    page = where
    page_args = list(args)
    if query.after is not None:
        joined = " AND " if where else " WHERE "
        anchor = conn.execute(
            f"SELECT x.created_at FROM outputs x{where}{joined}x.output_id = %s",
            [*args, query.after],
        ).fetchone()
        if anchor is None:
            raise AfterNotListedError(query.after)
        page = f"{where}{joined}(x.created_at, x.output_id) < (%s, %s)"
        page_args += [anchor["created_at"], query.after]
    rows = conn.execute(
        f"SELECT x.output_id FROM outputs x{page}"
        " ORDER BY x.created_at DESC, x.output_id DESC LIMIT %s",
        [*page_args, query.limit],
    ).fetchall()
    return [r["output_id"] for r in rows], total
