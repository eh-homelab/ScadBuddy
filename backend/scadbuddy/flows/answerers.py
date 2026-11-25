"""Who besides a person may answer a flow's question (#7471, spec 2026-11-25 §4.2)."""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import StringConstraints

#: ``starter``, ``sessions`` or ``token:<id>``, the id a lowercase UUID as
#: `ai_mcp_tokens.id` is compared.
AnswererRule = Annotated[
    str,
    StringConstraints(
        pattern=r"^(starter|sessions|token:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
    ),
]

#: The most rules the setting holds.
MAX_ANSWERERS: Final = 100
