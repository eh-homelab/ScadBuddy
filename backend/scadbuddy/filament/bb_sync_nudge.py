"""The Bambuddy instance a sync pass belongs to (#6265, plan
`docs/superpowers/plans/2026-11-18-filament-bb-sync.md` Decision 12)."""

from __future__ import annotations

import hashlib


def instance_for(url: str) -> str:
    """The instance id of the Bambuddy at ``url``: the first 12 hex of the SHA-256 of the
    URL without a trailing slash, in lower case. The links belong to one instance, and a
    pass for another one plans nothing (Decision 12)."""
    return hashlib.sha256(url.rstrip("/").lower().encode()).hexdigest()[:12]
