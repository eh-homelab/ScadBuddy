"""Sending a Bambuddy write as its clients do (#1143)."""

from __future__ import annotations

import uuid


def press() -> dict[str, str]:
    """One deliberate press: a fresh ``Idempotency-Key``, which every operation route
    requires."""
    return {"Idempotency-Key": uuid.uuid4().hex}
