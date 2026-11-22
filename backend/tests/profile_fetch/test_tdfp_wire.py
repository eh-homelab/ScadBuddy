"""#7025: the 3DFP search and create asks and answers the API, the worker and the browser
service share (`wire.py`), and the duplicate rule the create stops on (`duplicates.py`)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.profile_fetch import wire
from scadbuddy.profile_fetch.duplicates import likely_duplicates
from scadbuddy.profile_fetch.wire import Candidate, CreateAsk, SearchAsk


def _candidate(**values: object) -> Candidate:
    fields: dict[str, object] = {
        "tdfp_id": 1708,
        "brand": "SUNLU",
        "material": "PLA+/Pro",
        "material_type": "Silk",
        "name": "Bronze",
        "hexes": ["#689471"],
    }
    fields.update(values)
    return Candidate.model_validate(fields)


def _ask(**values: object) -> CreateAsk:
    fields: dict[str, object] = {
        "brand": "SUNLU",
        "material": "PLA+/Pro",
        "material_type": "Silk",
        "colour": "Bronze",
        "hexes": ["#779E7C"],
    }
    fields.update(values)
    return CreateAsk.model_validate(fields)


def test_the_query_is_the_brand_type_and_colour() -> None:
    """R-7025-2: only ``?q=`` filters, and the measured query was "SUNLU Silk Bronze"."""
    ask = SearchAsk(brand="SUNLU", material="PLA+/Pro", material_type="Silk", colour="Bronze")
    assert wire.search_query(ask) == "SUNLU Silk Bronze"
    assert wire.search_query(SearchAsk(brand="Inland")) == "Inland"


def test_candidates_matching_the_material_and_type_come_first() -> None:
    ask = SearchAsk(brand="SUNLU", material="PLA+/Pro", material_type="Silk", colour="Bronze")
    rows = [
        _candidate(tdfp_id=8916, material="PLA"),
        _candidate(tdfp_id=1, material_type="Silk+"),
        _candidate(tdfp_id=1708),
    ]
    assert [c.tdfp_id for c in wire.ranked(ask, rows)] == [1708, 1, 8916]


@pytest.mark.parametrize(
    ("name", "grams"),
    [("BRONZE (250g)", 250), ("Black (1kg)", 1000), ("Red (0.5 kg)", 500), ("Bronze", None)],
)
def test_the_nominal_weight_is_read_from_the_name(name: str, grams: int | None) -> None:
    """R-7025-3: the site appends the weight to the name; the list has no weight column."""
    assert wire.weight_from_name(name) == grams


def test_hexes_are_upper_case_and_at_most_four() -> None:
    assert _candidate(hexes=["#779e7c"]).hexes == ["#779E7C"]
    with pytest.raises(ValidationError):
        _ask(hexes=[])
    with pytest.raises(ValidationError):
        _ask(hexes=["#000000"] * 5)
    with pytest.raises(ValidationError):
        _ask(hexes=["red"])


def test_the_identifiers_are_the_well_known_keys() -> None:
    """R-7025-6: the create takes #7026's keys and stores none of them."""
    ask = _ask(
        mpn="SL-PLA-BRZ",
        gtin="06975373321234",
        asin="B0BN7FG4H7",
        manufacturer_url="https://www.sunlu.com/products/x",
        empty_spool_weight_g=120,
        td=2.5,
    )
    assert ask.identifiers() == {
        "mpn": "SL-PLA-BRZ",
        "gtin": "06975373321234",
        "asin": "B0BN7FG4H7",
        "manufacturer_url": "https://www.sunlu.com/products/x",
        "empty_spool_weight_g": "120",
        "td": "2.5",
    }
    with pytest.raises(ValidationError):
        _ask(manufacturer_url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        _ask(sku="x")


def test_a_same_named_filament_of_the_brand_material_and_type_is_a_likely_duplicate() -> None:
    """R-7025-5: same brand, material and type, and a close name or hex."""
    dup = likely_duplicates(_ask(), [_candidate()], "")
    assert [c.tdfp_id for c, _ in dup if c] == [1708]
    assert "name" in dup[0][1]


def test_the_weight_suffix_and_case_do_not_hide_a_duplicate() -> None:
    dup = likely_duplicates(_ask(colour="bronze"), [_candidate(name="BRONZE (250g)")], "")
    assert len(dup) == 1


def test_a_close_hex_alone_is_a_likely_duplicate() -> None:
    dup = likely_duplicates(
        _ask(colour="Antique Copper", hexes=["#689472"]), [_candidate(name="Bronze")], ""
    )
    assert [reason for _, reason in dup] == ["a colour within ΔE 5 of #689472"]


def test_another_material_or_type_or_brand_is_no_duplicate() -> None:
    rows = [
        _candidate(tdfp_id=8916, material="PLA"),
        _candidate(tdfp_id=2, material_type="Silk+"),
        _candidate(tdfp_id=3, brand="eSUN"),
        _candidate(tdfp_id=4, name="Olive Green", hexes=["#263E0F"]),
    ]
    assert likely_duplicates(_ask(), rows, "") == []


def test_a_candidate_the_person_cleared_is_no_duplicate() -> None:
    assert likely_duplicates(_ask(not_duplicates=[1708]), [_candidate()], "") == []


def test_a_field_the_site_did_not_show_does_not_clear_a_duplicate() -> None:
    """The duplicate list's cards may lack the breadcrumb: unknown is not different."""
    dup = likely_duplicates(_ask(), [_candidate(brand=None, material=None, material_type=None)], "")
    assert len(dup) == 1


def test_a_list_with_no_cards_stops_when_its_text_names_the_colour() -> None:
    """With no card read from the list, its text decides: the colour named there stops."""
    assert likely_duplicates(_ask(), [], "SUNLU PLA+/Pro Silk BRONZE (250g)") != []
    assert likely_duplicates(_ask(), [], "No similar filaments") == []
