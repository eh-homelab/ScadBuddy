"""Calling an operation route as ScadBuddy's clients do (#1143): every one (model,
library, print and Bambuddy writes) requires an ``Idempotency-Key`` since #1777."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient
from httpx import Response


def press() -> dict[str, str]:
    """One deliberate press: a fresh ``Idempotency-Key``, which every operation route
    requires."""
    return {"Idempotency-Key": uuid.uuid4().hex}


def accepted(send: Callable[[dict[str, str]], Response]) -> Response:
    """``send``'s answer once accepted: sent again with the same key while it is still
    being accepted (#3926, #4364), since on a loaded runner a short deadline passes first
    and the 503 means "send it again", as the browser's ``command()`` does."""
    from scadbuddy.api.operations import STILL_ACCEPTING_PROBLEM
    from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE

    key = press()
    resend_until = time.monotonic() + 60
    # Only a problem-JSON 503 is read as JSON, so any other answer (a proxy's HTML page
    # included) fails on its own text.
    while (
        (answer := send(key)).status_code == 503
        and answer.headers.get("content-type", "").startswith(PROBLEM_MEDIA_TYPE)
        and answer.json().get("type") == STILL_ACCEPTING_PROBLEM
    ):
        assert time.monotonic() < resend_until, answer.text
    return answer


def subjects(client: TestClient, kind: str) -> list[str]:
    """The subjects the operations record holds for ``kind``, oldest first (#3856)."""
    from scadbuddy.api.deps import STATE_ATTR
    from scadbuddy.operations.component import OPERATIONS

    pool = getattr(client.app.state, STATE_ATTR).components.get(OPERATIONS).store.pool  # type: ignore[attr-defined]
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT subject FROM operations WHERE kind = %s ORDER BY created_at", (kind,)
        ).fetchall()
    return [row["subject"] for row in rows]
