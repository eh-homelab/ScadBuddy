"""Calling an operation route as ScadBuddy's clients do (#1143): every one (model,
library, print and Bambuddy writes) requires an ``Idempotency-Key`` since #1777."""

from __future__ import annotations

import uuid


def press() -> dict[str, str]:
    """One deliberate press: a fresh ``Idempotency-Key``, which every operation route
    requires."""
    return {"Idempotency-Key": uuid.uuid4().hex}
