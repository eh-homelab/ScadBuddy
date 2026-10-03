"""Fixtures shared by the rack tests (#836)."""

from __future__ import annotations

import pytest

from scadbuddy.rack.rank import NOZZLE_MATERIALS

#: A code the tests treat as hardened; the shipped table is empty (spec §8).
HARD = "HS99"


@pytest.fixture
def hardened_code(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setitem(NOZZLE_MATERIALS, HARD, "hardened steel")
    monkeypatch.setitem(NOZZLE_MATERIALS, "HS01", "brass")
    return HARD
