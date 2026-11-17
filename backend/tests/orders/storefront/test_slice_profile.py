"""The shop's slice profile (#6153 PR C, #6574): a printer and a filament preset per
material, and the slice request a storefront variant is sliced with."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.api.storefront_slice import profile_errors
from scadbuddy.bambuddy.models import NozzleChoice, PresetRef
from scadbuddy.bambuddy.resolver import PrintChoices
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.storefront.listings import check_sliceable
from scadbuddy.orders.storefront.slice_profile import (
    ProfileMissingMaterialError,
    StorefrontSliceProfile,
    missing_materials,
    profile_digest,
    slice_request,
)
from scadbuddy.orders.storefront.wire import ListingInput
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS

CHOICES = PrintChoices(nozzles=[NozzleChoice(size="0.4", flow="high_flow")])
PLA = PresetRef(source="standard", id="Bambu PLA Basic @BBL H2C")
PETG = PresetRef(source="standard", id="Bambu PETG HF @BBL H2C")
PROFILE = StorefrontSliceProfile(printer="p-1", materials={"PLA": PLA, "petg": PETG})


def test_each_slot_gets_its_materials_preset_and_no_spool() -> None:
    """#6574: a slot is sliced as a quote slices a filament that is not loaded: its
    material's preset as the slot's Advanced preset, and no spool, so the slot keeps the
    render's own colour (each slot its own, so the slicer plans the real purges)."""
    request = slice_request(PROFILE, ["PLA", "PETG", "PLA"], CHOICES)
    assert request.filament_plan.slots == []
    assert request.choices.filament_overrides == {1: PLA, 2: PETG, 3: PLA}
    # The dialog's choices are kept as they are, the presets added.
    assert request.choices.model_copy(update={"filament_overrides": {}}) == CHOICES
    assert request.printer == "p-1"
    # The template's own process and support settings (#6153): the profile sets none.
    assert request.path is None
    assert request.supports is None
    assert request.prime_tower is None
    assert request.print_sequence is None
    assert request.part_settings is None


def test_material_is_casefolded() -> None:
    assert PROFILE.materials == {"pla": PLA, "petg": PETG}
    request = slice_request(PROFILE, ["Petg"], CHOICES)
    assert request.choices.filament_overrides == {1: PETG}


def test_missing_material_raises() -> None:
    with pytest.raises(ProfileMissingMaterialError) as raised:
        slice_request(PROFILE, ["PLA", "TPU"], CHOICES)
    assert raised.value.material == "TPU"


def test_profile_has_no_process_support_or_spool_field() -> None:
    with pytest.raises(ValidationError):
        StorefrontSliceProfile.model_validate(
            {"printer": "p-1", "materials": {}, "supports": {"enabled": True}}
        )
    # #6574: the old shape, a tray per material, is not read.
    with pytest.raises(ValidationError):
        StorefrontSliceProfile.model_validate({"printer": "p-1", "spools": {"pla": -1}})


def test_two_materials_that_fold_to_one_are_refused() -> None:
    with pytest.raises(ValidationError):
        StorefrontSliceProfile(printer="p-1", materials={"PLA": PLA, "pla": PETG})


def test_the_digest_follows_the_content_only() -> None:
    same = StorefrontSliceProfile(printer="p-1", materials={"petg": PETG, "pla": PLA})
    assert profile_digest(same) == profile_digest(PROFILE)
    moved = StorefrontSliceProfile(printer="p-1", materials={"pla": PETG, "petg": PETG})
    assert profile_digest(moved) != profile_digest(PROFILE)
    other = StorefrontSliceProfile(printer="p-2", materials={"pla": PLA, "petg": PETG})
    assert profile_digest(other) != profile_digest(PROFILE)


def test_missing_materials_skip_support_and_need_no_profile() -> None:
    materials = ["PLA", "PETG", "PVA", "Support W", "TPU", "tpu"]
    assert missing_materials(PROFILE, materials) == ["TPU"]
    # No profile: the listing is shown with no price (#6153), so nothing is missing.
    assert missing_materials(None, materials) == []


# -- the check at publish (#6153 PR C, Task C2) ------------------------------------------

MATERIALS = {"red": "PLA", "green": "PETG", "white": "PVA", "grey": "Support for PLA"}
PLA_ONLY = StorefrontSliceProfile(printer="p-1", materials={"pla": PLA})


def listing(*colours: str) -> ListingInput:
    return ListingInput(slug="demo", commit="a" * 40, title="Demo", colours=list(colours))


def test_publish_refuses_material_without_preset() -> None:
    with pytest.raises(ApiError) as refused:
        check_sliceable(PLA_ONLY, listing("red", "green", "white", "grey"), MATERIALS)
    assert refused.value.type == STOREFRONT_PROBLEMS + "listing_material_unsliceable"
    assert refused.value.status == 422
    # Support materials need no preset: support stays the shop's (spec §4).
    assert refused.value.extensions["materials"] == ["PETG"]


def test_publish_with_a_preset_for_each_material_is_allowed() -> None:
    check_sliceable(PLA_ONLY, listing("red", "white"), MATERIALS)


def test_profile_with_unknown_printer_is_refused() -> None:
    assert profile_errors(PLA_ONLY, known=False) == [
        {"loc": ["body", "storefront_slice_profile", "printer"], "msg": "no printer p-1"}
    ]
    assert profile_errors(PLA_ONLY, known=True) == []


def test_publish_without_profile_is_allowed() -> None:
    # No profile: the listing is shown with no price.
    check_sliceable(None, listing("red", "green"), MATERIALS)
