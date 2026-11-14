"""What the CRUD routes and their clients share (#5298): the callers, the paths and the
headers. It imports no database or app code, so the storefront edge (#5282), which holds
no database, can use `crud/client.py`."""

from __future__ import annotations

from typing import Literal

#: The callers with CRUD routes; `auth/routes.py` `CRUD_CLASSES` has the same keys.
CrudCaller = Literal["projects", "render", "print", "storefront", "agent", "intake"]
CRUD_HEADER = "X-ScadBuddy-Crud"
CRUD_HEADERS = {CRUD_HEADER: "1", "Cache-Control": "no-store"}
#: The reason of the 503 a CRUD route answers on a `psycopg.OperationalError`.
DATABASE_UNAVAILABLE = "database-unavailable"
#: The reason of a 5xx an operator's setting causes (the storefront's store is not S3):
#: no retry fixes it, so a client answers it at once (`client.py`).
NOT_CONFIGURED = "not_configured"
#: Each caller's CRUD routes are under ``/api/v1`` plus ``PREFIX/<caller>``.
PREFIX = "/internal/crud"
