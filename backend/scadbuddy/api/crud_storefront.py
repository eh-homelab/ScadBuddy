"""The storefront edge's CRUD routes (#5282, spec 2026-11-02 §5.1): under
``/api/v1/internal/storefront/``, for the ``storefront`` credential only (`crud_router`).
Its ``GET …/areas`` is `api/crud_areas.py`'s; each area's task adds its routes here.

The edge sends the visitor id it verified in the cookie (`VISITOR_HEADER`) and the
browser's address (`CLIENT_HEADER`). This side cannot check the cookie's mac: it takes
the id only as a 256-bit secret to look a visitor up by, and the address only to key the
per-client render limit (plan "Trust model"). `visitor_id` and `store_client` are the
dependencies that read them.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Final

from fastapi import Request

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.storefront.wire import (
    CLIENT_HEADER,
    STORE_CLIENT_PROBLEM,
    VISITOR_HEADER,
    VISITOR_ID_PROBLEM,
)

router = crud_router("storefront")

#: 32 random bytes as URL-safe base64, unpadded (`secrets.token_urlsafe(32)`).
VISITOR_ID: Final = re.compile(r"[A-Za-z0-9_-]{43}")


def visitor_id(request: Request) -> str:
    """The visitor id the edge sends; 400 ``…/storefront/visitor_id`` unless it is one."""
    value = request.headers.get(VISITOR_HEADER, "")
    if VISITOR_ID.fullmatch(value) is None:
        raise ApiError(
            400,
            f"{VISITOR_HEADER} must be a visitor id of 43 URL-safe characters.",
            type_=VISITOR_ID_PROBLEM,
        )
    return value


def store_client(request: Request) -> str:
    """The browser's address the edge sends; 400 ``…/storefront/store_client`` unless it
    is one IP address."""
    value = request.headers.get(CLIENT_HEADER, "")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise ApiError(
            400,
            f"{CLIENT_HEADER} must be one IP address.",
            type_=STORE_CLIENT_PROBLEM,
        ) from None
    return value
