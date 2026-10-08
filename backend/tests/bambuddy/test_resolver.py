"""Spec §4 — the resolver, from hand-built catalogues and the H2C recording.

Spec §4.1 (after §5's live tests) overrides the original plan: Bambuddy rejects
every ``source: "local"`` printer preset, so ``printer_preset_name`` always names
Bambu's own preset and High Flow / mixed nozzle sizes are handled without ever
inventing one.
"""

from __future__ import annotations

import pytest

from scadbuddy.bambuddy.catalogue import PresetChoice, _Catalogue, _choice
from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, SlotNeed, SpoolOption
from scadbuddy.bambuddy.models import PresetCatalogue, PresetRef, SlotChoice, SpoolFilamentPreset
from scadbuddy.bambuddy.resolver import (
    TIERS,
    NozzleChoice,
    PrintChoices,
    printer_preset_name,
    resolve,
)
from tests.bambuddy.conftest import recording

H2C = "Bambu Lab H2C {} nozzle"


def row(source: str, id_: str, name: str, *sizes: str, kind: str | None = None) -> PresetChoice:
    return PresetChoice(
        ref=PresetRef(source=source, id=id_),  # type: ignore[arg-type]
        name=name,
        filament_type=kind,
        compatible_printers=[H2C.format(size) for size in sizes],
    )


def recorded() -> _Catalogue:
    catalogue = PresetCatalogue.model_validate(recording("slicer-presets-h2c.json"))
    tiers = (catalogue.cloud, catalogue.standard)
    return _Catalogue(
        printer=[_choice(p) for tier in tiers for p in tier.printer],
        process=[_choice(p) for tier in tiers for p in tier.process],
        filament=[_choice(p) for tier in tiers for p in tier.filament],
    )


def _not_cloud(rows: list[PresetChoice]) -> list[PresetChoice]:
    return [row for row in rows if row.ref.source != "cloud"]


def without_cloud() -> _Catalogue:
    whole = recorded()
    return _Catalogue(
        _not_cloud(whole.printer), _not_cloud(whole.process), _not_cloud(whole.filament)
    )


def spool(spool_id: int, material: str, preset: str | None, name: str | None) -> SpoolOption:
    return SpoolOption(
        spool_id=spool_id,
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
        {
            4: [
                SpoolFilamentPreset(
                    printer_model="H2C", nozzle_diameter="0.2", slicer_filament="34"
                )
            ]
        },
    )
    assert resolved.filament_presets == [PresetRef(source="local", id="34")]


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
        PrintChoices(
            nozzles=[NozzleChoice(size="0.4", flow="high_flow"), NozzleChoice(size="0.4")]
        ),
        recorded(),
        {},
    )
    assert resolved.printer_preset == PresetRef(source="cloud", id="GM041")
    assert resolved.warnings == []
    assert resolved.errors == []


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
