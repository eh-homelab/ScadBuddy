"""A support material (#6152, #7195 review): `SUPPORT_MATERIAL` against the shared vectors,
which the page's (`frontend/src/storefront/params.ts`) and the agent's
(`agent/src/tools/storefrontDraft.ts`) copies are tested against too."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scadbuddy.orders.storefront.wire import SUPPORT_MATERIAL

VECTORS = Path(__file__).resolve().parents[4] / "test-fixtures/storefront/support-materials.json"
_CASES: dict[str, list[str]] = json.loads(VECTORS.read_text())


@pytest.mark.parametrize("material", _CASES["support"])
def test_a_support_material(material: str) -> None:
    assert SUPPORT_MATERIAL.search(material)


@pytest.mark.parametrize("material", _CASES["customer"])
def test_a_customer_material(material: str) -> None:
    assert not SUPPORT_MATERIAL.search(material)
