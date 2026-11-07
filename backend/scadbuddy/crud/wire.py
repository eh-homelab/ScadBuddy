"""What the CRUD routes and their clients share (#5298): the callers, the paths and the
headers. It imports no database or app code, so the storefront edge (#5282), which holds
no database, can use `crud/client.py`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

#: The callers with CRUD routes; `auth/routes.py` `CRUD_CLASSES` has the same keys.
CrudCaller = Literal["projects", "render", "print", "storefront"]
CRUD_HEADER = "X-ScadBuddy-Crud"
CRUD_HEADERS = {CRUD_HEADER: "1", "Cache-Control": "no-store"}
#: The reason of the 503 a CRUD route answers on a `psycopg.OperationalError`.
DATABASE_UNAVAILABLE = "database-unavailable"
PREFIX = "/internal/crud"
#: Each caller's CRUD routes, under ``/api/v1``: ``/internal/crud/<caller>``, except the
#: storefront's, at spec 2026-11-02 §5.1's path.
CRUD_PREFIXES: Mapping[CrudCaller, str] = {
    "projects": f"{PREFIX}/projects",
    "render": f"{PREFIX}/render",
    "print": f"{PREFIX}/print",
    "storefront": "/internal/storefront",
}
