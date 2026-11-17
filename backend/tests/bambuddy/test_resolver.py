"""Spec §4 — the resolver, from hand-built catalogues and the H2C recording.

Spec §4.1 (after §5's live tests) overrides the original plan: Bambuddy rejects
every ``source: "local"`` printer preset, so ``printer_preset_name`` always names
Bambu's own preset and High Flow / mixed nozzle sizes are handled without ever
inventing one.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import cast
from uuid import uuid4

import pytest

from scadbuddy.bambuddy.catalogue import Catalogue, PresetChoice, _choice
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, SlotNeed, SpoolOption
from scadbuddy.bambuddy.models import PresetCatalogue, PresetRef, SlotChoice, SpoolFilamentPreset
from scadbuddy.bambuddy.print_run import resolve_plates
from scadbuddy.bambuddy.resolver import (
    TIERS,
    NozzleChoice,
    PrintChoices,
    printer_preset_name,
    resolve,
)
from scadbuddy.bambuddy.trays import tray_spool_id
from scadbuddy.filament.models import EffectivePreset
from scadbuddy.slicing.catalogue import native_catalogue
from scadbuddy.slicing.profiles import Profiles
from tests.bambuddy.conftest import recording
from tests.slicing.conftest import PROFILES

H2C = "Bambu Lab H2C {} nozzle"


def _preset(nozzle: str, filament: str) -> EffectivePreset:
    """An SB spool's effective preset for the H2C (#6262 F1)."""
    return EffectivePreset(
        printer_model="H2C",
        nozzle_diameter=nozzle,
        slicer_filament=filament,
        slicer_filament_name=None,
        overridden=True,
    )


def row(source: str, id_: str, name: str, *sizes: str, kind: str | None = None) -> PresetChoice:
    return PresetChoice(
        ref=PresetRef(source=source, id=id_),  # type: ignore[arg-type]
        name=name,
        filament_type=kind,
        compatible_printers=[H2C.format(size) for size in sizes],
    )


def recorded() -> Catalogue:
    catalogue = PresetCatalogue.model_validate(recording("slicer-presets-h2c.json"))
    tiers = (catalogue.cloud, catalogue.standard)
    return Catalogue(
        printer=[_choice(p) for tier in tiers for p in tier.printer],
        process=[_choice(p) for tier in tiers for p in tier.process],
        filament=[_choice(p) for tier in tiers for p in tier.filament],
    )


def _not_cloud(rows: list[PresetChoice]) -> list[PresetChoice]:
    return [row for row in rows if row.ref.source != "cloud"]


def without_cloud() -> Catalogue:
    whole = recorded()
    return Catalogue(
        _not_cloud(whole.printer), _not_cloud(whole.process), _not_cloud(whole.filament)
    )


def spool(spool_id: int, material: str, preset: str | None, name: str | None) -> SpoolOption:
    return SpoolOption(
        spool_id=spool_id,
        sb_number=spool_id,
        material=material,
        colour="#112233",
        slicer_filament=preset,
        slicer_filament_name=name,
    )


def options(*spools: SpoolOption) -> FilamentOptions:
    return FilamentOptions(
        library_file_id=41,
        slots=[SlotNeed(slot_id=i + 1, colour="#FFFFFF") for i in range(len(spools))],
        spools=list(spools),
    )


def plan(*spool_ids: int) -> FilamentPlan:
    return FilamentPlan(
        slots=[SlotChoice(slot_id=i + 1, spool_id=s) for i, s in enumerate(spool_ids)]
    )


BASIC = spool(1, "PLA", "GFA00", "Bambu PLA Basic")
PETG = spool(2, "PETG", "GFG00", "Bambu PETG Basic")
TPU = spool(3, "TPU", None, None)
THIRD_PARTY = spool(4, "PLA", "51", "Insignia PLA @H2C")


@pytest.mark.parametrize("size", ["0.2", "0.4", "0.6", "0.8"])
@pytest.mark.parametrize("tier", ["fine", "standard", "draft"])
def test_every_tier_names_a_process_bambu_ships_for_that_size(size: str, tier: str) -> None:
    catalogue = recorded()
    name = TIERS[size][tier]
    hits = [p for p in catalogue.process if p.name == name]
    assert hits, f"{name} is not in the recorded H2C catalogue"
    assert all(H2C.format(size) in p.compatible_printers for p in hits)


def test_the_0_2_fine_tier_is_the_0_08_high_quality_process() -> None:
    resolved = resolve(
        options(BASIC),
        plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")], tier="fine"),
        recorded(),
        {},
    )
    assert resolved.process_preset == PresetRef(source="cloud", id="GP243")
    assert resolved.printer_preset == PresetRef(source="cloud", id="GM042")
    assert resolved.errors == []


def test_a_bambu_spool_resolves_to_its_size_specific_preset_by_name() -> None:
    """Review focus 1: ``GFA00`` is a base id; the 0.2 profile is found by name."""
    resolved = resolve(
        options(BASIC),
        plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")]),
        recorded(),
        {},
    )
    assert resolved.filament_presets == [PresetRef(source="cloud", id="GFSA00_23")]


def test_a_plate_using_only_slot_2_pads_slot_1_rather_than_compacting() -> None:
    """A plate using only slot 2 (plate 2 of an all_plates run, commonly)
    must still hand Bambuddy a length-2 array with the spool's preset at index 1, not a
    length-1 array that slices the wrong preset onto the wrong filament."""
    built = FilamentOptions(
        library_file_id=41,
        slots=[SlotNeed(slot_id=2, colour="#FFFFFF")],
        spools=[BASIC],
    )
    resolved = resolve(
        built,
        FilamentPlan(slots=[SlotChoice(slot_id=2, spool_id=1)]),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")]),
        recorded(),
        {},
    )
    assert resolved.filament_presets == [
        PresetRef(source="cloud", id="GFSA00_23"),
        PresetRef(source="cloud", id="GFSA00_23"),
    ]
    assert resolved.filament_colours == ["#FFFFFF", "#112233"]


def test_slots_1_and_3_pad_only_the_gap_at_slot_2() -> None:
    built = FilamentOptions(
        library_file_id=41,
        slots=[SlotNeed(slot_id=1, colour="#FFFFFF"), SlotNeed(slot_id=3, colour="#FFFFFF")],
        spools=[BASIC, PETG],
    )
    resolved = resolve(
        built,
        FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=1), SlotChoice(slot_id=3, spool_id=2)]),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")]),
        recorded(),
        {},
    )
    assert len(resolved.filament_presets) == 3
    assert resolved.filament_presets[0] == PresetRef(source="cloud", id="GFSA00_23")
    assert resolved.filament_presets[2] == PresetRef(source="cloud", id="GFSG00_24")
    # The padding slot (2) borrows the first used slot's preset, mirroring the old
    # slice_filament_presets rather than inventing an id Bambuddy cannot look up.
    assert resolved.filament_presets[1] == resolved.filament_presets[0]
    assert resolved.filament_colours == ["#112233", "#FFFFFF", "#112233"]


def test_without_the_cloud_tier_everything_resolves_through_standard() -> None:
    """Review focus 2: Bambu Cloud logged out."""
    resolved = resolve(
        options(BASIC),
        plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")], tier="fine"),
        without_cloud(),
        {},
    )
    assert resolved.printer_preset == PresetRef(source="standard", id="Bambu Lab H2C 0.2 nozzle")
    assert resolved.process_preset is not None
    assert resolved.process_preset.id == "0.08mm High Quality @BBL H2C 0.2 nozzle"
    assert resolved.filament_presets[0].id == "Bambu PLA Basic @BBL H2C 0.2 nozzle"


def test_a_spools_own_per_nozzle_row_wins_over_everything_but_an_override() -> None:
    catalogue = recorded()
    catalogue.filament.append(row("local", "34", "Insignia PLA @H2C 0.2n", "0.2", kind="PLA"))
    resolved = resolve(
        options(THIRD_PARTY),
        plan(4),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")]),
        catalogue,
        {4: [_preset("0.2", "34")]},
    )
    assert resolved.filament_presets == [PresetRef(source="local", id="34")]


def test_an_own_preset_bambuddys_catalogue_lacks_is_found_by_its_name() -> None:
    """#6262 Task 7: a pulled Bambuddy spool's preset is its BBL filament_id ("GFA00"),
    which Bambuddy's catalogue rows do not carry; its name finds Bambu's preset, as the
    Bambuddy spool's own name did before."""
    bare = spool(1, "PLA", None, None)
    own = _preset("0.4", "GFA00").model_copy(update={"slicer_filament_name": "Bambu PLA Basic"})
    resolved = resolve(
        options(bare),
        plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.4")]),
        recorded(),
        {1: [own]},
    )
    named = resolve(
        options(BASIC), plan(1), PrintChoices(nozzles=[NozzleChoice(size="0.4")]), recorded(), {}
    )
    assert resolved.filament_presets == named.filament_presets
    assert "Generic" not in resolved.filament_presets[0].id
    assert resolved.warnings == []


def test_an_advanced_override_wins() -> None:
    """``GFSL99_22`` is Generic PLA for a 0.2 nozzle: in the catalogue and fitting the
    chosen size, so it beats the spool's own ``GFSA00_23``."""
    override = PresetRef(source="cloud", id="GFSL99_22")
    resolved = resolve(
        options(BASIC),
        plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")], filament_overrides={1: override}),
        recorded(),
        {},
    )
    assert resolved.filament_presets == [override]
    assert resolved.errors == []


def test_an_override_bambuddy_does_not_have_is_a_slot_error() -> None:
    """PR #335 review 2: a preset since deleted in Bambuddy is refused here, naming the
    slot, rather than failing opaquely inside Bambuddy's slice."""
    resolved = resolve(
        options(BASIC, PETG),
        plan(1, 2),
        PrintChoices(
            nozzles=[NozzleChoice(size="0.2")],
            filament_overrides={2: PresetRef(source="cloud", id="GONE404")},
        ),
        recorded(),
        {},
    )
    assert [(e.kind, e.slot_id) for e in resolved.errors] == [("no-preset", 2)]
    assert resolved.errors[0].message == (
        "The preset chosen for slot 2 isn't one Bambuddy has any more. Pick another under Advanced."
    )


def test_an_override_for_another_nozzle_size_is_a_slot_error() -> None:
    """A stale override left from an earlier size: ``GFSG99_18`` is Generic PETG for a
    0.4 nozzle, so it cannot slice for a 0.2."""
    resolved = resolve(
        options(PETG),
        plan(2),
        PrintChoices(
            nozzles=[NozzleChoice(size="0.2")],
            filament_overrides={1: PresetRef(source="cloud", id="GFSG99_18")},
        ),
        recorded(),
        {},
    )
    assert [(e.kind, e.slot_id) for e in resolved.errors] == [("no-preset", 1)]
    assert resolved.errors[0].message == (
        "The preset chosen for slot 1 doesn't fit a 0.2 mm nozzle. Pick another under Advanced."
    )


def test_with_nothing_of_its_own_a_spool_falls_back_to_generic_for_its_material() -> None:
    """The recorded cloud id for ``Generic PETG @BBL H2C 0.2 nozzle`` is ``GFSG99_20``
    (checked with ``jq`` against the recording — not the ``GFSG99_22`` the brief guessed)."""
    resolved = resolve(
        options(spool(5, "PETG", None, None)),
        plan(5),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")]),
        recorded(),
        {},
    )
    assert resolved.filament_presets[0] == PresetRef(source="cloud", id="GFSG99_20")
    assert [w.kind for w in resolved.warnings] == ["no-preset"]


def test_tpu_at_0_2_is_a_slot_error_not_a_guess() -> None:
    """Review focus 3."""
    resolved = resolve(
        options(TPU),
        plan(3),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")]),
        recorded(),
        {},
    )
    assert [(e.kind, e.slot_id) for e in resolved.errors] == [("no-preset", 1)]


def test_a_used_slot_with_no_spool_chosen_says_so_rather_than_pointing_at_advanced() -> None:
    """Final review 1: a slot only a later plate uses had no picker row, so it arrived
    with no spool. The old "no slicer preset ... Pick one under Advanced." was advice
    Advanced could not act on; the error names the real gap."""
    built = FilamentOptions(
        library_file_id=41,
        slots=[SlotNeed(slot_id=1, colour="#FFFFFF"), SlotNeed(slot_id=2, colour="#FF1493")],
        spools=[BASIC],
    )
    resolved = resolve(
        built,
        FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=1)]),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")]),
        recorded(),
        {},
    )
    assert [(e.kind, e.slot_id, e.message) for e in resolved.errors] == [
        ("no-choice", 2, "Slot 2 has no spool chosen.")
    ]


def test_every_slot_erroring_leaves_no_filament_presets_to_send() -> None:
    """With nothing resolved there is no preset to pad with, so the array is empty —
    never ``None``-padded — and every used slot carries its own error, which is what
    makes the caller refuse before Bambuddy sees the array."""
    built = FilamentOptions(
        library_file_id=41,
        slots=[SlotNeed(slot_id=1, colour="#FFFFFF"), SlotNeed(slot_id=2, colour="#FF1493")],
        spools=[BASIC],
    )
    resolved = resolve(
        built,
        FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=1)]),
        PrintChoices(
            nozzles=[NozzleChoice(size="0.2")],
            filament_overrides={1: PresetRef(source="cloud", id="GONE404")},
        ),
        recorded(),
        {},
    )
    assert resolved.filament_presets == []
    assert [(e.kind, e.slot_id) for e in resolved.errors] == [("no-preset", 1), ("no-choice", 2)]


def test_mixed_sizes_are_always_an_error() -> None:
    """Spec §4.1: no ``allow_mixed_sizes`` escape hatch — Bambuddy can't slice it."""
    nozzles = [NozzleChoice(size="0.2"), NozzleChoice(size="0.4")]
    resolved = resolve(options(BASIC), plan(1), PrintChoices(nozzles=nozzles), recorded(), {})
    assert [e.kind for e in resolved.errors] == ["mixed-sizes"]
    assert "can't slice mixed" in resolved.errors[0].message


@pytest.mark.parametrize(
    "flows",
    [
        ("standard", "standard"),
        ("high_flow", "standard"),
        ("standard", "high_flow"),
        ("high_flow", "high_flow"),
    ],
)
def test_hf_on_either_side_still_names_bambus_own_printer_preset(
    flows: tuple[str, str],
) -> None:
    """Spec §4.1: Bambuddy 400s on ScadBuddy's own printer presets, so there is no
    "ScadBuddy · ..." name for any flow combination — only Bambu's own preset name."""
    nozzles = [NozzleChoice(size="0.4", flow=f) for f in flows]  # type: ignore[arg-type]
    assert printer_preset_name(nozzles) == "Bambu Lab H2C 0.4 nozzle"


def test_high_flow_names_bambus_own_preset_with_no_warning() -> None:
    """#484: the flow is stated in the 3MF (``replate_3mf``), not by a preset, so High
    Flow resolves as Standard does and is no longer said to slice as Standard."""
    resolved = resolve(
        options(BASIC),
        plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.4", flow="high_flow")]),
        recorded(),
        {},
    )
    assert resolved.printer_preset == PresetRef(source="cloud", id="GM041")
    assert resolved.warnings == []
    assert resolved.errors == []


@pytest.mark.parametrize("high_flow_side", [0, 1])
def test_high_flow_on_one_side_and_standard_on_the_other_is_refused(high_flow_side: int) -> None:
    """#4272: print #355 stalled on the H2C with the left High Flow and the right
    Standard. Either way round, the choice is refused with the reason."""
    nozzles = [NozzleChoice(size="0.4"), NozzleChoice(size="0.4")]
    nozzles[high_flow_side] = NozzleChoice(size="0.4", flow="high_flow")
    resolved = resolve(options(BASIC), plan(1), PrintChoices(nozzles=nozzles), recorded(), {})
    assert [error.kind for error in resolved.errors] == ["mixed-flows"]
    assert "choose one flow for both" in resolved.errors[0].message


def test_an_advanced_process_name_wins_over_the_tier() -> None:
    resolved = resolve(
        options(BASIC),
        plan(1),
        PrintChoices(
            nozzles=[NozzleChoice(size="0.4")],
            tier="standard",
            process_name="0.08mm High Quality @BBL H2C",
        ),
        recorded(),
        {},
    )
    assert resolved.process_preset == PresetRef(source="cloud", id="GP244")


def test_a_process_for_another_size_is_refused() -> None:
    resolved = resolve(
        options(BASIC),
        plan(1),
        PrintChoices(
            nozzles=[NozzleChoice(size="0.4")],
            process_name="0.08mm High Quality @BBL H2C 0.2 nozzle",
        ),
        recorded(),
        {},
    )
    assert [e.kind for e in resolved.errors] == ["no-process"]


def test_colours_and_bed_type_pass_through() -> None:
    resolved = resolve(
        options(BASIC),
        plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.4")], bed_type="Cool Plate"),
        recorded(),
        {},
    )
    assert resolved.filament_colours == ["#112233"]
    assert resolved.bed_type == "Cool Plate"


# --- SB spools' presets (#6262 Task 5) --------------------------------------------------


def _with_petg(catalogue: Catalogue) -> Catalogue:
    """The image's test catalogue with a PETG preset per nozzle whose ``filament_id`` is
    the tray's ``tray_info_idx`` below."""
    petg = [
        PresetChoice(
            ref=PresetRef(source="standard", id=f"Tray PETG @BBL H2C {size} nozzle"),
            name=f"Tray PETG @BBL H2C {size} nozzle",
            compatible_printers=[H2C.format(size)],
            filament_id="GFG00",
        )
        for size in ("0.4", "0.6")
    ]
    return dataclasses.replace(catalogue, filament=[*catalogue.filament, *petg])


def _tray_holding(number: int | None) -> FilamentOptions:
    tray = SpoolOption(
        spool_id=tray_spool_id(0, 1),
        material="PLA",
        colour="#FFFFFF",
        slicer_filament="GFG00",
        tray_only=True,
        sb_number=number,
        sb_spool_id=None if number is None else uuid4(),
    )
    return FilamentOptions(
        library_file_id=None, slots=[SlotNeed(slot_id=1, colour="#FFFFFF")], spools=[tray]
    )


def test_a_trays_preset_comes_from_its_spool() -> None:
    """The SB spool in the tray (#7) names its preset per nozzle (F1): on 0.4 it is the
    spool's, over the tray's ``tray_info_idx``; on 0.6 it has none, so the tray's."""
    catalogue = _with_petg(native_catalogue(Profiles(PROFILES)))
    held = _tray_holding(7)
    tray_plan = plan(tray_spool_id(0, 1))
    own = {7: [_preset("0.4", "Bambu PLA Basic @BBL H2C")]}

    on_04 = resolve(
        held, tray_plan, PrintChoices(nozzles=[NozzleChoice(size="0.4")]), catalogue, own
    )
    on_06 = resolve(
        held, tray_plan, PrintChoices(nozzles=[NozzleChoice(size="0.6")]), catalogue, own
    )

    assert on_04.filament_presets == [PresetRef(source="standard", id="Bambu PLA Basic @BBL H2C")]
    assert on_06.filament_presets == [
        PresetRef(source="standard", id="Tray PETG @BBL H2C 0.6 nozzle")
    ]


class _Reads:
    """`SpoolPresetSource` over fixed answers, recording what it was asked."""

    def __init__(
        self, presets: dict[int, list[EffectivePreset]], bb: dict[int, int] | None = None
    ) -> None:
        self._presets, self._bb = presets, bb or {}
        self.asked: list[list[int]] = []

    async def presets(self, numbers: Sequence[int]) -> dict[int, list[EffectivePreset]]:
        self.asked.append(sorted(numbers))
        return {n: self._presets[n] for n in numbers if n in self._presets}

    async def bb_spools(self, numbers: Sequence[int]) -> dict[int, int]:
        return {n: self._bb[n] for n in numbers if n in self._bb}


class _Bambuddy:
    """Only what `resolve_plates` may ask Bambuddy: a spool's presets."""

    def __init__(self, presets: dict[int, list[SpoolFilamentPreset]]) -> None:
        self._presets = presets
        self.asked: list[int] = []

    async def spool_filament_presets(self, spool_id: int) -> list[SpoolFilamentPreset]:
        self.asked.append(spool_id)
        return self._presets.get(spool_id, [])


def _sb_row(number: int) -> FilamentOptions:
    spool = SpoolOption(spool_id=number, sb_number=number, material="PLA", colour="#FFFFFF")
    return FilamentOptions(
        library_file_id=41, slots=[SlotNeed(slot_id=1, colour="#FFFFFF")], spools=[spool]
    )


@pytest.mark.parametrize(
    ("size", "asked", "preset"),
    [("0.4", [], "GFSA00_22"), ("0.6", [16], "GFSA00_31")],
)
async def test_a_bb_preset_is_read_only_without_an_sb_one(
    size: str, asked: list[int], preset: str
) -> None:
    """SB spool #7 is linked to Bambuddy's spool 16. Its own 0.4 row is used and Bambuddy
    is not asked; for 0.6 it has none, so Bambuddy's row for 0.6 is read (F1)."""
    reads = _Reads({7: [_preset("0.4", "GFSA00_22")]}, bb={7: 16})
    bambuddy = _Bambuddy(
        {
            16: [
                SpoolFilamentPreset(
                    printer_model="H2C", nozzle_diameter="0.6", slicer_filament="GFSA00_31"
                )
            ]
        }
    )
    (resolved,), errors = await resolve_plates(
        cast(BambuddyClient, bambuddy),
        [1],
        [_sb_row(7)],
        plan(7),
        PrintChoices(nozzles=[NozzleChoice(size=size)]),  # type: ignore[arg-type]
        recorded(),
        presets=reads,
    )
    assert errors == [] and reads.asked == [[7]]
    assert bambuddy.asked == asked
    assert resolved.filament_presets == [PresetRef(source="cloud", id=preset)]


async def test_native_options_read_no_bambuddy() -> None:
    """A native run has no client: a linked SB spool with no row for the nozzle falls
    back to the tray's preset, and nothing asks Bambuddy (no ``TypeError``)."""
    reads = _Reads({}, bb={7: 16})
    (resolved,), errors = await resolve_plates(
        None,
        [1],
        [_tray_holding(7)],
        plan(tray_spool_id(0, 1)),
        PrintChoices(nozzles=[NozzleChoice(size="0.4")]),
        _with_petg(native_catalogue(Profiles(PROFILES))),
        presets=reads,
    )
    assert errors == [] and reads.asked == [[7]]
    assert resolved.filament_presets == [
        PresetRef(source="standard", id="Tray PETG @BBL H2C 0.4 nozzle")
    ]
