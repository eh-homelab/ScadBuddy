"""The orders problem types (phase 1 plan; spec 2026-10-18 §4): one mapping of a
`LifecycleError` to the route's answer, shared by the routes (``api/orders.py``) and the
quote's run (``orders/quote.py``), so the two cannot drift."""

from __future__ import annotations

from typing import Any

from fastapi import status

from scadbuddy.core.problems import ApiError
from scadbuddy.orders.lifecycle import LifecycleError

PROBLEMS = "https://scadbuddy.dev/problems/orders/"


def refused(error: LifecycleError) -> ApiError:
    """A lifecycle refusal as the routes answer it: 422 naming the body field for an
    unprocessable one, else 404 or 409 under the problem type
    ``https://scadbuddy.dev/problems/orders/<slug>``, with the items and shipments it names."""
    if error.unprocessable:
        return ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(error),
            errors=[{"loc": ["body", error.slug], "msg": str(error)}],
        )
    extensions: dict[str, Any] = {}
    if error.items:
        extensions["items"] = error.items
    if error.shipments:
        extensions["shipments"] = error.shipments
    return ApiError(
        status.HTTP_404_NOT_FOUND if error.not_found else status.HTTP_409_CONFLICT,
        str(error),
        type_=PROBLEMS + error.slug,
        **extensions,
    )
