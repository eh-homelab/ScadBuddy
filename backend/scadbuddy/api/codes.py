"""`GET /api/v1/codes/{code}` (#6955): what a typed code (`L-12`, `S-142`, `T-4`, `I-9`, `K-3`)
names. A label's QR opens `/x/<code>`, which asks this."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, status

from scadbuddy.api.components import component_dep
from scadbuddy.codes.parse import parse_code
from scadbuddy.codes.resolve import Resolved, resolve
from scadbuddy.core.problems import ApiError
from scadbuddy.rows.component import ROWS, Rows

router = APIRouter(prefix="/codes", tags=["codes"])

RowsDep = Annotated[Rows, component_dep(ROWS)]


def _resolve(rows: Rows, text: str) -> Resolved | None:
    code = parse_code(text)
    if code is None:
        return None
    rows.pool.open(wait=False)
    with rows.pool.connection() as conn:
        return resolve(conn, code)


@router.get(
    "/{code}",
    response_model=Resolved,
    summary="Resolve a typed code",
    description=(
        "`L-<n>` is a location, `S-<n>` a spool, `T-<n>` a spool type (`kind` "
        "`spool_type`), `I-<n>` an item and `K-<n>` an item kind (`kind` "
        "`item_kind`, `id` the kind's text id), in any case, with or without the dash. "
        "An archived thing still resolves, with `archived` set. 404 with `code` "
        "`code_unknown` for a code that does not parse, has an unknown prefix or names no row."
    ),
)
async def get_code(rows: RowsDep, code: str) -> Resolved:
    found = await asyncio.to_thread(_resolve, rows, code)
    if found is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"no thing has the code {code!r}", code="code_unknown"
        )
    return found
