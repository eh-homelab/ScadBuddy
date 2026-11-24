"""Bambuddy's local presets on a native run (#7244, the owner's decision of 2026-11-23):
the native slicer reads none, from a copy or from Bambuddy. A binding or an override
that still names one is refused and said, never sliced from Bambuddy."""

from __future__ import annotations

import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, SlotNeed, SpoolOption
from scadbuddy.bambuddy.models import PresetRef, SlotChoice
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices, resolve
from scadbuddy.bambuddy.trays import tray_spool_id
from scadbuddy.filament.models import EffectivePreset
from scadbuddy.slicing.catalogue import catalogue_for
from scadbuddy.slicing.presets import build_presets, needs_bambuddy
from scadbuddy.slicing.profiles import ProfileError, Profiles
from scadbuddy.slicing.values import native_values
from tests.slicing.conftest import PROFILES

TRAY = tray_spool_id(0, 0)


def h2c_choices(**kwargs: object) -> PrintChoices:
    return PrintChoices(nozzles=[NozzleChoice(size="0.4")], **kwargs)  # type: ignore[arg-type]


def tray(sb_number: int | None = None) -> FilamentOptions:
    """A tray chosen for itself, holding SB spool ``sb_number`` (#6262) or none."""
    return FilamentOptions(
        library_file_id=None,
        slots=[SlotNeed(slot_id=1, colour="#FFFFFF")],
        spools=[
            SpoolOption(
                spool_id=TRAY,
                material="PLA",
                colour="#000000",
                tray_only=True,
                sb_number=sb_number,
            )
        ],
    )


def tray_plan() -> FilamentPlan:
    return FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=TRAY)])


def bound(preset: str) -> list[EffectivePreset]:
    """An SB spool's H2C 0.4 preset naming ``preset``."""
    return [
        EffectivePreset(
            printer_model="H2C",
            nozzle_diameter="0.4",
            slicer_filament=preset,
            slicer_filament_name=None,
            overridden=False,
        )
    ]


@respx.mock
@pytest.mark.parametrize("slot", ["printer_preset", "process_preset", "filament_presets"])
async def test_a_bambuddy_local_preset_is_refused_and_never_read(
    plan: SlicePlan, slot: str
) -> None:
    """With a client too: respx refuses any route not mocked here, so Bambuddy is not
    asked for the preset."""
    ref = PresetRef(source="local", id="7")
    local = plan.model_copy(update={slot: [ref] if slot == "filament_presets" else ref})
    profiles = Profiles(PROFILES)
    assert not await needs_bambuddy(local, profiles)
    async with BambuddyClient(BambuddyConfig(base_url="https://bambuddy.test", api_key="k")) as c:
        with pytest.raises(ProfileError, match=r"cannot read Bambuddy local \w+ preset 7"):
            await build_presets(local, profiles, c)


async def test_a_bambuddy_local_filament_refusal_says_to_import(plan: SlicePlan) -> None:
    local = plan.model_copy(update={"filament_presets": [PresetRef(source="local", id="7")]})
    with pytest.raises(ProfileError, match=r"Import presets from Bambuddy now"):
        await build_presets(local, Profiles(PROFILES), None)


async def test_the_native_catalogue_offers_no_bambuddy_local_preset() -> None:
    catalogue = await catalogue_for(None, native=Profiles(PROFILES))
    assert not [row for row in catalogue.filament if row.ref.source == "local"]
    seven = PresetRef(source="local", id="7")
    assert catalogue.native_ref(seven, "filament") is None


async def test_a_bambuddy_local_override_is_refused_saying_to_import() -> None:
    resolved = resolve(
        tray(),
        tray_plan(),
        h2c_choices(filament_overrides={1: PresetRef(source="local", id="7")}),
        await catalogue_for(None, native=Profiles(PROFILES)),
        {},
    )
    (error,) = resolved.errors
    assert "Bambuddy local preset, which ScadBuddy no longer reads" in error.message


async def test_a_spool_bound_to_a_bambuddy_local_preset_is_said_not_sliced() -> None:
    """A binding the start's adopt has not turned into an ``sb-`` ref is an error the
    dialog shows (#7244), never a fallback to Bambuddy or to a preset by name."""
    resolved = resolve(
        tray(16),
        tray_plan(),
        h2c_choices(),
        await catalogue_for(None, native=Profiles(PROFILES)),
        {16: bound("7")},
    )
    (error,) = resolved.errors
    assert error.kind == "no-preset" and error.slot_id == 1
    assert "Bambuddy local preset 7, which ScadBuddy no longer reads" in error.message
    assert "Import Bambuddy's presets" in error.message


async def test_a_bambuddy_local_process_has_no_values() -> None:
    values = await native_values(Profiles(PROFILES))(PresetRef(source="local", id="9"))
    assert not values.resolved
