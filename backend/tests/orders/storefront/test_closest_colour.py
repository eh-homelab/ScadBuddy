"""The closest colour (#6258): the shared vectors, which the page's twin reads too, and
CIEDE2000 against Sharma, Wu and Dalal's published test pairs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.orders.storefront.closest_colour import Swatch, ciede2000, closest_colour

VECTORS = Path(__file__).resolve().parents[4] / "test-fixtures/storefront/closest-colour.json"


def _swatch(raw: dict[str, Any]) -> Swatch:
    return Swatch(hex=raw["hex"], name=raw["name"], alpha=raw.get("alpha"))


def _vectors() -> list[dict[str, Any]]:
    vectors: list[dict[str, Any]] = json.loads(VECTORS.read_text())
    return vectors


def test_the_shared_file_has_the_cases_it_must() -> None:
    assert len(_vectors()) >= 12


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: str(v["case"]))
def test_a_shared_vector(vector: dict[str, Any]) -> None:
    candidates = [_swatch(c) for c in vector["candidates"]]
    assert closest_colour(_swatch(vector["target"]), candidates) == vector["want"]


def test_no_candidates_is_none() -> None:
    assert closest_colour(Swatch(hex="#FFFFFF", name=None), []) is None


# "The CIEDE2000 Color-Difference Formula: Implementation Notes, Supplementary Test Data,
# and Mathematical Observations", Sharma, Wu and Dalal (2005), Table 1, pairs 1, 7, 17, 25:
# https://hajim.rochester.edu/ece/sites/gsharma/ciede2000/ciede2000noteCRNA.pdf
@pytest.mark.parametrize(
    ("first", "second", "want"),
    [
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
        ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ],
)
def test_ciede2000_meets_the_published_pairs(
    first: tuple[float, float, float], second: tuple[float, float, float], want: float
) -> None:
    assert ciede2000(first, second) == pytest.approx(want, abs=1e-4)
