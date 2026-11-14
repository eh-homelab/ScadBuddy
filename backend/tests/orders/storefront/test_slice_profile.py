"""The shop's slice profile (#6153 PR C): a printer and a spool per material, and the
slice request a storefront variant is sliced with."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.api.storefront_slice import profile_errors
from scadbuddy.bambuddy.models import NozzleChoice
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

CHOICES = PrintChoices(nozzles=[NozzleChoice(size="0.4")])
PROFILE = StorefrontSliceProfile(printer="p-1", spools={"PLA": 11, "petg": -3})


def test_slice_request_one_spool_per_slot_by_material() -> None:
    request = slice_request(PROFILE, ["PLA", "PETG", "PLA"], CHOICES)
    assert [(s.slot_id, s.spool_id) for s in request.filament_plan.slots] == [
        (1, 11),
        (2, -3),
        (3, 11),
    ]
    assert request.printer == "p-1"
    assert request.choices == CHOICES
    # The template's own process and support settings (#6153): the profile sets none.
    assert request.path is None
    assert request.supports is None
    assert request.prime_tower is None
    assert request.print_sequence is None
    assert request.part_settings is None


def test_material_is_casefolded() -> None:
    assert PROFILE.spools == {"pla": 11, "petg": -3}
    request = slice_request(PROFILE, ["Petg"], CHOICES)
    assert request.filament_plan.slots[0].spool_id == -3


def test_missing_material_raises() -> None:
    with pytest.raises(ProfileMissingMaterialError) as raised:
        slice_request(PROFILE, ["PLA", "TPU"], CHOICES)
    assert raised.value.material == "TPU"


def test_profile_has_no_process_or_support_field() -> None:
    with pytest.raises(ValidationError):
        StorefrontSliceProfile.model_validate(
            {"printer": "p-1", "spools": {}, "supports": {"enabled": True}}
        )


def test_two_materials_that_fold_to_one_are_refused() -> None:
    with pytest.raises(ValidationError):
        StorefrontSliceProfile(printer="p-1", spools={"PLA": 1, "pla": 2})


def test_the_digest_follows_the_content_only() -> None:
    same = StorefrontSliceProfile(printer="p-1", spools={"petg": -3, "pla": 11})
    assert profile_digest(same) == profile_digest(PROFILE)
    moved = StorefrontSliceProfile(printer="p-1", spools={"pla": 12, "petg": -3})
    assert profile_digest(moved) != profile_digest(PROFILE)
    other = StorefrontSliceProfile(printer="p-2", spools={"pla": 11, "petg": -3})
    assert profile_digest(other) != profile_digest(PROFILE)


def test_missing_materials_skip_support_and_need_no_profile() -> None:
    materials = ["PLA", "PETG", "PVA", "Support W", "TPU", "tpu"]
    assert missing_materials(PROFILE, materials) == ["TPU"]
    # No profile: the listing is shown with no price (#6153), so nothing is missing.
    assert missing_materials(None, materials) == []


# -- the check at publish (#6153 PR C, Task C2) ------------------------------------------

MATERIALS = {"red": "PLA", "green": "PETG", "white": "PVA", "grey": "Support for PLA"}
PLA_ONLY = StorefrontSliceProfile(printer="p-1", spools={"pla": 7})


def listing(*colours: str) -> ListingInput:
    return ListingInput(slug="demo", commit="a" * 40, title="Demo", colours=list(colours))


def test_publish_refuses_material_without_spool() -> None:
    with pytest.raises(ApiError) as refused:
        check_sliceable(PLA_ONLY, listing("red", "green", "white", "grey"), MATERIALS)
    assert refused.value.type == STOREFRONT_PROBLEMS + "listing_material_unsliceable"
    assert refused.value.status == 422
    # Support materials need no spool: support stays the shop's (spec §4).
    assert refused.value.extensions["materials"] == ["PETG"]


def test_publish_with_a_spool_for_each_material_is_allowed() -> None:
    check_sliceable(PLA_ONLY, listing("red", "white"), MATERIALS)


def test_profile_with_unknown_spool_is_refused() -> None:
    profile = StorefrontSliceProfile(printer="p-1", spools={"PLA": 7, "PETG": 8})
    assert profile_errors(profile, {7, 9}) == [
        {
            "loc": ["body", "storefront_slice_profile", "spools", "petg"],
            "msg": "the printer offers no spool 8 for petg",
        }
    ]
    assert profile_errors(profile, {7, 8}) == []


def test_profile_with_unknown_printer_is_refused() -> None:
    assert profile_errors(PLA_ONLY, None) == [
        {"loc": ["body", "storefront_slice_profile", "printer"], "msg": "no printer p-1"}
    ]


def test_publish_without_profile_is_allowed() -> None:
    # No profile: the listing is shown with no price.
    check_sliceable(None, listing("red", "green"), MATERIALS)
