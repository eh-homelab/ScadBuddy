"""ScadBuddy's own filament presets on a native run (#7176): offered, resolved and sliced
from ScadBuddy's store, never from Bambuddy; and on Bambuddy's path, sent by their
mirror's Bambuddy id."""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

import pytest

from scadbuddy.bambuddy.catalogue import Catalogue, PresetChoice
from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.resolver import resolve
from scadbuddy.filament.models import EffectivePreset
from scadbuddy.slicing.catalogue import catalogue_for
from scadbuddy.slicing.own_presets import OwnRow, facts
from scadbuddy.slicing.presets import build_presets, needs_bambuddy
from scadbuddy.slicing.profiles import ProfileError, Profiles
from tests.slicing.conftest import PROFILES, MemoryHeld
from tests.slicing.test_held_presets import PLA, h2c_choices, tray, tray_plan

H2C = "Bambu Lab H2C 0.4 nozzle"

#: A fetched 3DFP preset: flattened, every option written out, ``inherits`` empty.
SILK: dict[str, Any] = {
    "type": "filament",
    "name": "SUNLU PLA+ Silk (3DFP 35902) @H2C",
    "inherits": "Bambu PLA Basic @BBL H2C",
    "filament_type": ["PLA"],
    "default_filament_colour": ["#C0C0C0"],
    "compatible_printers": [H2C],
    "nozzle_temperature": ["220"],
    "nozzle_temperature_range_low": ["205"],
}


class MemoryOwn:
    """`OwnPresets` in memory."""

    def __init__(self, docs: dict[int, dict[str, Any]], bb: dict[int, int] | None = None):
        self.docs = docs
        self.bb = bb or {}
        self.asked: list[list[int]] = []

    async def rows(self) -> list[OwnRow]:
        out = []
        for preset_id, doc in self.docs.items():
            filament_type, colour, printers = facts(doc)
            out.append(
                OwnRow(
                    id=preset_id,
                    name=doc["name"],
                    sha256=str(hash(json.dumps(doc, sort_keys=True))),
                    filament_type=filament_type,
                    filament_colour=colour,
                    compatible_printers=printers,
                    bb_preset_id=self.bb.get(preset_id),
                )
            )
        return out

    async def documents(self, ids: Collection[int]) -> dict[int, dict[str, Any]]:
        self.asked.append(sorted(ids))
        return {i: self.docs[i] for i in ids if i in self.docs}


@pytest.fixture
def own() -> MemoryOwn:
    return MemoryOwn({12: SILK})


def _plan(plan: SlicePlan, ref: str) -> SlicePlan:
    return plan.model_copy(update={"filament_presets": [PresetRef(source="local", id=ref)]})


async def test_a_scadbuddy_preset_slices_from_its_store(own: MemoryOwn, plan: SlicePlan) -> None:
    profiles = Profiles(PROFILES, own=own)
    files = await build_presets(_plan(plan, "sb-12"), profiles, None)
    flat = json.loads(files.filaments[0])
    assert flat["nozzle_temperature"] == ["220"]
    assert flat["nozzle_temperature_range_low"] == ["205"]
    assert flat["name"] == SILK["name"] and "inherits" not in flat
    # Keyed by the file it built: a new version slices again.
    assert set(files.from_bambuddy) == {"filament_1"}
    assert not await needs_bambuddy(_plan(plan, "sb-12"), profiles)
    own.docs[12] = {**SILK, "nozzle_temperature": ["225"]}
    again = await build_presets(_plan(plan, "sb-12"), profiles, None)
    assert again.from_bambuddy != files.from_bambuddy


async def test_a_scadbuddy_preset_that_is_gone_is_refused(plan: SlicePlan) -> None:
    profiles = Profiles(PROFILES, own=MemoryOwn({}))
    with pytest.raises(ProfileError, match=r"ScadBuddy filament preset sb-9: ScadBuddy has no"):
        await build_presets(_plan(plan, "sb-9"), profiles, None)
    with pytest.raises(ProfileError, match=r"reads no ScadBuddy presets"):
        await build_presets(_plan(plan, "sb-9"), Profiles(PROFILES), None)


async def test_the_catalogue_lists_a_preset_once(own: MemoryOwn) -> None:
    """The kept copy's row of the Bambuddy id a ScadBuddy preset has (its mirror) is left
    out: one preset, one row."""
    own.bb[12] = 7
    profiles = Profiles(PROFILES, held=MemoryHeld(PLA), own=own)
    catalogue = await catalogue_for(None, native=profiles)
    local = [row for row in catalogue.filament if row.ref.source == "local"]
    assert [
        (row.ref.id, row.name, row.filament_type, row.compatible_printers) for row in local
    ] == [("sb-12", SILK["name"], "PLA", [H2C])]
    ref = PresetRef(source="local", id="sb-12")
    assert catalogue.native_ref(ref, "filament") == ref


async def test_a_spool_naming_a_scadbuddy_preset_resolves_natively(own: MemoryOwn) -> None:
    preset = EffectivePreset(
        printer_model="H2C",
        nozzle_diameter="0.4",
        slicer_filament="sb-12",
        slicer_filament_name=SILK["name"],
        overridden=False,
    )
    resolved = resolve(
        tray(16),
        tray_plan(),
        h2c_choices(),
        await catalogue_for(None, native=Profiles(PROFILES, own=own)),
        {16: [preset]},
    )
    assert resolved.errors == []
    assert resolved.filament_presets == [PresetRef(source="local", id="sb-12")]


def test_bambuddys_path_takes_the_mirrors_id() -> None:
    """Bambuddy's catalogue knows the preset by its mirror's local id only."""
    mirrored = PresetChoice(
        ref=PresetRef(source="local", id="141"),
        name=SILK["name"],
        filament_type="PLA",
        compatible_printers=[H2C],
    )
    catalogue = Catalogue(printer=[], process=[], filament=[mirrored])

    def preset(bambuddy_id: str | None) -> EffectivePreset:
        return EffectivePreset(
            printer_model="H2C",
            nozzle_diameter="0.4",
            slicer_filament="sb-12",
            slicer_filament_name=None,
            overridden=False,
            bambuddy_id=bambuddy_id,
        )

    assert preset("141").for_bambuddy() == "141"
    assert preset(None).for_bambuddy() is None
    resolved = resolve(tray(16), tray_plan(), h2c_choices(), catalogue, {16: [preset("141")]})
    assert resolved.filament_presets == [PresetRef(source="local", id="141")]
    other = EffectivePreset(
        printer_model="H2C",
        nozzle_diameter="0.4",
        slicer_filament="GFSA00",
        slicer_filament_name=None,
        overridden=False,
    )
    assert other.for_bambuddy() == "GFSA00"
