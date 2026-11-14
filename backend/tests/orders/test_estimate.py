"""The rough estimate (#5282, spec 2026-11-02 §5.4, plan point 4): a price from a render's
part volumes, every line of a quote's formula but machine time, which a render does not
know. ``orders/estimate.py`` holds what the back office's ``GET /orders/estimate`` and the
storefront's estimate share."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import trimesh

from scadbuddy.orders.density import DensityTable
from scadbuddy.orders.estimate import (
    EstimatedPart,
    StoreEstimate,
    filament_estimate,
    part_volumes,
    rough_estimate,
)
from scadbuddy.orders.models import Filament, PricingPolicy
from scadbuddy.orders.pricing import Plate, PlateFilament, PricingError, price
from scadbuddy.render.bambu3mf import PlateParts, write_plates_3mf
from scadbuddy.render.split import ColourPart
from scadbuddy.slicing.profiles import Profiles
from scadbuddy.storefront.routes import ESTIMATE_FIELDS
from tests.orders.test_density import PROFILES

NOW = datetime(2026, 11, 8, tzinfo=UTC)
POLICY = PricingPolicy(
    machine_rate_per_hour="1.50", setup_fee="0.75", labour_per_item="2", markup_percent="15"
)


def filament(fid: str, colour: str, cost: str, density: str | None = "1.24") -> Filament:
    return Filament(
        id=fid,
        material="PLA",
        subtype=None,
        brand=None,
        colour_name=None,
        colour=colour,
        cost_per_kg=cost,
        density_g_cm3=density,
        archived_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


RED = filament("red", "#FF0000", "25.50")
BLUE = filament("blue", "#0000FF", "20")


@pytest.fixture
def table() -> DensityTable:
    return DensityTable.build(Profiles(PROFILES))


def unsliced() -> list[Plate]:
    return [Plate(index=1, print_seconds=0, filaments=[PlateFilament(slot=1, used_g=Decimal(10))])]


def test_an_estimated_price_has_no_machine_line() -> None:
    priced = price(unsliced(), {1: RED}, POLICY, currency="USD", quantity=1, estimated=True)
    assert [line.kind for line in priced.lines] == ["filament", "setup", "labour", "markup"]


def test_a_quote_still_needs_the_seconds() -> None:
    with pytest.raises(PricingError):
        price(unsliced(), {1: RED}, POLICY, currency="USD", quantity=1)


def part(plate: int, n: int, colour: str, volume: float | None) -> EstimatedPart:
    return EstimatedPart(plate=plate, part=n, name=f"p{n}", colour=colour, volume_mm3=volume)


def test_the_rough_estimate_prices_each_colour_with_its_filament() -> None:
    parts = [
        part(1, 1, "#FF0000", 8000.0),
        part(1, 2, "#0000ff", 1000.0),
        part(2, 1, "#FF0000", 9000.0),
        part(2, 2, "#FF0000", None),
    ]
    by_colour = {"#ff0000": RED, "#0000ff": BLUE}
    est = rough_estimate(
        parts, lambda c: by_colour[c.casefold()], POLICY, currency="USD", factor="1.15"
    )
    assert est.estimate is True and est.unknown_parts == 1
    # Red: 8 cm3 and 9 cm3 x 1.24 x 1.15 = 11.41 g + 12.83 g; blue 1 cm3 = 1.43 g.
    assert est.grams == "25.67"
    kinds = [line.kind for line in est.lines]
    assert kinds == ["filament", "filament", "setup", "setup", "labour", "markup"]
    filament_lines = [line.amount_minor for line in est.lines if line.kind == "filament"]
    # 24.24 g x 25.50 / 1000 = 0.618 -> 62; 1.43 g x 20 / 1000 = 0.0286 -> 3.
    assert filament_lines == [62, 3]
    assert est.filament_minor == 65
    assert est.price_minor == sum(line.amount_minor for line in est.lines)
    assert est.price_minor > est.filament_minor


def test_a_filament_with_no_density_has_no_estimate() -> None:
    plain = filament("plain", "#FF0000", "20", density=None)
    with pytest.raises(ValueError, match="density"):
        rough_estimate(
            [part(1, 1, "#FF0000", 1000.0)], lambda _c: plain, POLICY, currency="USD", factor="1"
        )


def test_a_filament_with_no_density_takes_its_profiles(table: DensityTable) -> None:
    """#6136: a Bambu PLA Basic record with no density weighs at Bambu PLA Basic's 1.26,
    and the estimate says so; one with its own density keeps it."""
    basic = filament("basic", "#FF0000", "20", density=None).model_copy(
        update={"subtype": "Basic", "brand": "Bambu Lab"}
    )
    by_colour = {"#ff0000": basic, "#0000ff": BLUE}
    est = rough_estimate(
        [part(1, 1, "#FF0000", 10000.0), part(1, 2, "#0000FF", 10000.0)],
        lambda c: by_colour[c.casefold()],
        POLICY,
        currency="USD",
        factor="1",
        densities=table,
    )
    # 10 cm3 x 1.26 + 10 cm3 x 1.24.
    assert est.grams == "25.00"
    assert [(d.filament_id, d.density_g_cm3, d.source, d.profile) for d in est.densities] == [
        ("basic", "1.26", "profile", "Bambu PLA Basic @base"),
        ("blue", "1.24", "book", None),
    ]
    alone = filament_estimate(
        [part(1, 1, "#FF0000", 10000.0)], basic, currency="USD", factor="1", densities=table
    )
    assert (alone.grams, alone.densities[0].source) == ("12.60", "profile")
    # A visitor never reads which density, as before.
    assert "densities" not in est.public().model_dump()


def test_no_source_still_has_no_estimate(table: DensityTable) -> None:
    tri = filament("tri", "#FF0000", "20", density=None).model_copy(
        update={"subtype": "Galaxy", "brand": "Bambu Lab"}
    )
    with pytest.raises(ValueError, match="no slicer profile"):
        filament_estimate(
            [part(1, 1, "#FF0000", 1.0)], tri, currency="USD", factor="1", densities=table
        )


def test_part_volumes_reads_every_plate(tmp_path: Path) -> None:
    model = tmp_path / "model.3mf"
    write_plates_3mf(
        [
            PlateParts(
                (ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(20, 20, 20))),),
                (1,),
            ),
            PlateParts(
                (ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(30, 30, 10))),),
                (1,),
            ),
        ],
        ["#FF0000"],
        model,
        thumbnails=None,
    )
    parts = part_volumes(model, 2, [])
    assert [(p.plate, round(p.volume_mm3 or 0)) for p in parts] == [(1, 8000), (2, 9000)]


def test_the_edge_sends_on_every_public_estimate_field() -> None:
    """The edge's allowlist (`storefront/routes.py`) is the public shape, field for field."""
    assert set(StoreEstimate.model_fields) == ESTIMATE_FIELDS
