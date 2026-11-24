"""A storefront listing of a library file as the public site shows it (#6710, spec
2026-11-21 §6.1, §6.2): its source, its parts (the offered slots, "Part 1"…, each at the
listing colour closest to the file's own in the first material that can print it) and its
previews as edge paths. A support slot, an unused slot and a support filament never show."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from scadbuddy.orders.models import Filament
from scadbuddy.orders.storefront.listings import public_card, public_listing
from scadbuddy.orders.storefront.wire import FileSlot, FileSnapshot, Listing, PublicListing
from scadbuddy.store.content import ContentStore
from tests.orders.storefront.test_inputs import NOW, filament

SHA = "1" * 64
PLA_RED = "pla-red"
PLA_BLUE = "pla-blue"
PLA_PINK = "pla-pink"
PETG_GREEN = "petg-green"
PVA = "pva-white"
FILAMENTS = {
    PLA_RED: filament(PLA_RED, "#FF0000"),
    PLA_BLUE: filament(PLA_BLUE, "#0000FF"),
    PLA_PINK: filament(PLA_PINK, "#FF66CC"),
    PETG_GREEN: filament(PETG_GREEN, "#00FF00", "PETG"),
    PVA: filament(PVA, "#FFFFFF", "PVA"),
}
SHOP = ["PLA", "PETG"]


def slot(n: int, colour: str, *, used: bool = True, support: bool = False) -> FileSlot:
    return FileSlot(
        slot=n, colour=colour, material="PVA" if support else "PLA", used=used, support=support
    )


def file_listing(**fields: Any) -> Listing:
    snapshot = FileSnapshot(
        file_id=7,
        library="native",
        filename="keychain-red.3mf",
        file_type="3mf",
        sha256=SHA,
        size=100,
        copy_key=f"listing-file-{SHA}",
        slots=[
            slot(0, "#EE1111"),
            slot(1, "#FFFFFF", support=True),
            slot(2, "#123456", used=False),
            slot(3, "#2222DD"),
        ],
        plates=[1, 2],
        preview_keys=["listing-preview-" + "3" * 64, "listing-preview-" + "4" * 64],
        support={1: PVA},
    )
    return Listing.model_validate(
        {
            "id": "abcdefghijklmnop",
            "source": "library_file",
            "library_file": {"id": 7, "library": "native", "pinned": SHA},
            "title": "Keychain",
            "colours": [PLA_RED, PLA_BLUE, PLA_PINK, PETG_GREEN, PVA],
            "position": 0,
            "published": True,
            "snapshot": snapshot,
            "media": [],
            "published_at": NOW,
            "created_at": NOW,
            "updated_at": NOW,
            **fields,
        }
    )


def shown(
    listing: Listing, filaments: dict[str, Filament] | None = None, shop: list[str] | None = None
) -> PublicListing:
    # No media, so the content store is never read.
    return asyncio.run(
        public_listing(cast(ContentStore, None), listing, filaments or FILAMENTS, shop or SHOP)
    )


def test_a_file_listing_shows_its_parts_and_previews() -> None:
    public = shown(file_listing())
    assert public.source == "library_file"
    assert public.params == {}
    assert [(s.slot, s.label, s.default) for s in public.slots] == [
        (0, "Part 1", PLA_RED),
        (3, "Part 2", PLA_BLUE),
    ]
    assert public.previews == [
        "/api/store/v1/listings/abcdefghijklmnop/preview/1.glb",
        "/api/store/v1/listings/abcdefghijklmnop/preview/2.glb",
    ]
    assert public.available


def test_a_file_listing_never_shows_a_support_filament() -> None:
    public = shown(file_listing())
    assert [f.id for f in public.colours] == [PLA_RED, PLA_BLUE, PLA_PINK, PETG_GREEN]
    assert PVA not in {s.default for s in public.slots}


def test_each_part_starts_in_the_first_material_that_can_print_it() -> None:
    """§6.2: the first allowed material that offers a colour, then the closest colour."""
    public = shown(file_listing(materials=["PETG", "PLA"]))
    assert public.materials == ["PETG", "PLA"]
    assert [s.default for s in public.slots] == [PETG_GREEN, PETG_GREEN]


def test_a_part_with_no_colour_on_offer_is_unavailable() -> None:
    public = shown(file_listing(colours=[PVA]))
    assert not public.available
    assert [(s.slot, s.label, s.default) for s in public.slots] == [
        (0, "Part 1", None),
        (3, "Part 2", None),
    ]
    card = asyncio.run(
        public_card(cast(ContentStore, None), file_listing(colours=[PVA]), FILAMENTS, SHOP)
    )
    assert not card.available


def test_a_file_listing_with_no_preview_has_none() -> None:
    listing = file_listing()
    assert listing.file_snapshot is not None
    bare = listing.model_copy(
        update={"snapshot": listing.file_snapshot.model_copy(update={"preview_keys": []})}
    )
    assert shown(bare).previews == []


def test_the_card_of_a_file_listing_is_available() -> None:
    card = asyncio.run(public_card(cast(ContentStore, None), file_listing(), FILAMENTS, SHOP))
    assert card.available and card.title == "Keychain"


def test_a_snapshots_previews_are_one_per_plate_or_none() -> None:
    whole = {
        "file_id": 1,
        "library": "native",
        "filename": "a.3mf",
        "file_type": "3mf",
        "sha256": "a" * 64,
        "size": 1,
        "copy_key": "listing-file-" + "a" * 64,
        "slots": [],
        "plates": [1, 3],
    }
    assert FileSnapshot.model_validate(whole).previews == {}
    both = FileSnapshot.model_validate(whole | {"preview_keys": ["p1", "p3"]})
    assert both.previews == {1: "p1", 3: "p3"}
    with pytest.raises(ValueError, match="shorter"):
        _ = FileSnapshot.model_validate(whole | {"preview_keys": ["p1"]}).previews
