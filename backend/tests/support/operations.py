"""Calling an operation route as ScadBuddy's clients do (#1143): every one (model,
library, print and Bambuddy writes) requires an ``Idempotency-Key`` since #1777."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def press() -> dict[str, str]:
    """One deliberate press: a fresh ``Idempotency-Key``, which every operation route
    requires."""
    return {"Idempotency-Key": uuid.uuid4().hex}


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
