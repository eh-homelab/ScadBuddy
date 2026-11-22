"""The shop's slice profile (#6153 PR C, #6574): a printer and a filament type per
material, and the slice request a storefront variant is sliced with."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from scadbuddy.api.storefront_slice import profile_errors
from scadbuddy.bambuddy.models import NozzleChoice
from scadbuddy.bambuddy.part_settings import PartSettings
from scadbuddy.bambuddy.resolver import PrintChoices
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.models import EffectivePreset
from scadbuddy.library.settings_store import ModelPrintChoices
from scadbuddy.orders.storefront.listings import check_sliceable
from scadbuddy.orders.storefront.slice_profile import (
    ProfileMissingMaterialError,
    StorefrontSliceProfile,
    missing_materials,
    profile_digest,
    slice_request,
    slot_presets,
    slot_types,
)
from scadbuddy.orders.storefront.wire import ListingInput
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS

CHOICES = PrintChoices(nozzles=[NozzleChoice(size="0.4", flow="high_flow")])
PLA = UUID("00000000-0000-4000-8000-0000000000a1")
PETG = UUID("00000000-0000-4000-8000-0000000000a2")
PROFILE = StorefrontSliceProfile(printer="p-1", materials={"PLA": PLA, "petg": PETG})


def _preset(filament: str) -> EffectivePreset:
    return EffectivePreset(
        printer_model="H2C",
        nozzle_diameter="0.4",
        slicer_filament=filament,
        slicer_filament_name=None,
        overridden=False,
    )


def test_each_slot_gets_its_materials_type_and_no_spool() -> None:
    """#6574: a slot is sliced with its material's filament type's presets, and no spool,
    so the slot keeps the render's own colour (each slot its own, so the slicer plans the
    real purges)."""
    types = slot_types(PROFILE, ["PLA", "PETG", "PLA"])
    assert types == [PLA, PETG, PLA]
    rows = {PLA: [_preset("GFA00")], PETG: [_preset("GFG99")]}
    assert slot_presets(types, rows) == {1: rows[PLA], 2: rows[PETG], 3: rows[PLA]}
    # A type with no presets gives none, which the resolver refuses.
    assert slot_presets([PETG], {}) == {1: []}
    request = slice_request(PROFILE, CHOICES, None)
    assert request.filament_plan.slots == []
    # The dialog's choices are kept as they are.
    assert request.choices == CHOICES
    assert request.printer == "p-1"
    # The template's own process and support settings (#6153): the profile sets none.
    assert request.path is None
    assert request.supports is None
    assert request.prime_tower is None
    assert request.print_sequence is None
    assert request.part_settings is None
    assert request.process_overrides is None


def test_the_request_takes_the_setup_the_dialog_seeds_from_the_remembered_choices() -> None:
    """#6710 §7.2: the print sequence, the process overrides and the settings per part the
    dialog seeds from the remembered entry (`usePrintChoices.ts` `seedDialog`), as any
    Bambu Studio key through the one `process_overrides` (#2998)."""
    remembered = ModelPrintChoices(
        print_sequence="by object",
        process_overrides={"enable_support": "1", "brim_width": "4"},
        part_settings={1: PartSettings(wall_loops=4)},
        tier="fine",
    )
    request = slice_request(PROFILE, CHOICES, remembered)
    assert request.print_sequence == "by object"
    assert request.process_overrides == {"enable_support": "1", "brim_width": "4"}
    assert request.part_settings == {1: PartSettings(wall_loops=4)}
    # The opening choices are the caller's: the request keeps them as they are.
    assert request.choices == CHOICES
    # A remembered entry that names none of the three sets none.
    bare = slice_request(PROFILE, CHOICES, ModelPrintChoices(tier="fine"))
    assert (bare.print_sequence, bare.process_overrides, bare.part_settings) == (None, None, None)


def test_material_is_casefolded() -> None:
    assert PROFILE.materials == {"pla": PLA, "petg": PETG}
    assert slot_types(PROFILE, ["Petg"]) == [PETG]


def test_missing_material_raises() -> None:
    with pytest.raises(ProfileMissingMaterialError) as raised:
        slot_types(PROFILE, ["PLA", "TPU"])
    assert raised.value.material == "TPU"


def test_profile_has_no_process_support_spool_or_preset_field() -> None:
    with pytest.raises(ValidationError):
        StorefrontSliceProfile.model_validate(
            {"printer": "p-1", "materials": {}, "supports": {"enabled": True}}
        )
    # #6574: the old shape, a spool per material, is not read.
    with pytest.raises(ValidationError):
        StorefrontSliceProfile.model_validate({"printer": "p-1", "spools": {"pla": -1}})
    # Nor is a preset of its own: the type's presets are the one store.
    with pytest.raises(ValidationError):
        StorefrontSliceProfile.model_validate(
            {"printer": "p-1", "materials": {"pla": {"source": "standard", "id": "x"}}}
        )


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


def test_publish_refuses_material_without_a_type() -> None:
    with pytest.raises(ApiError) as refused:
        check_sliceable(PLA_ONLY, listing("red", "green", "white", "grey"), MATERIALS)
    assert refused.value.type == STOREFRONT_PROBLEMS + "listing_material_unsliceable"
    assert refused.value.status == 422
    # Support materials need none: support stays the shop's (spec §4).
    assert refused.value.extensions["materials"] == ["PETG"]


def test_publish_with_a_type_for_each_material_is_allowed() -> None:
    check_sliceable(PLA_ONLY, listing("red", "white"), MATERIALS)


def test_profile_with_unknown_printer_or_type_is_refused() -> None:
    assert profile_errors(PLA_ONLY, False, {PLA}) == [
        {"loc": ["body", "storefront_slice_profile", "printer"], "msg": "no printer p-1"}
    ]
    assert profile_errors(PLA_ONLY, True, {PLA}) == []
    assert profile_errors(PLA_ONLY, True, set()) == [
        {
            "loc": ["body", "storefront_slice_profile", "materials", "pla"],
            "msg": f"no filament type {PLA} for pla",
        }
    ]


def test_publish_without_profile_is_allowed() -> None:
    # No profile: the listing is shown with no price.
    check_sliceable(None, listing("red", "green"), MATERIALS)
