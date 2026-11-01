"""#2799 — a filament profile's filament settings (Silk PLA's 235 °C nozzle): written
into the matching slots' filament files on the native slicer, the channel measured to
change the G-code (``slicing/presets.py``), and said not to be applied on Bambuddy's,
whose slice request has no filament field."""

from __future__ import annotations

from scadbuddy.bambuddy.filament_profiles import (
    SILK,
    filament_not_applied,
    slot_filament_overrides,
)
from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, SlotNeed
from scadbuddy.bambuddy.filaments import SpoolOption as Spool
from scadbuddy.bambuddy.models import SlotChoice


def _options() -> FilamentOptions:
    return FilamentOptions(
        library_file_id=None,
        slots=[SlotNeed(slot_id=1), SlotNeed(slot_id=2), SlotNeed(slot_id=3)],
        spools=[
            Spool(spool_id=10, material="PLA", subtype="Basic"),
            Spool(spool_id=11, material="PLA", subtype="Silk+"),
            # Bambu's tri-colour silk: subtype Tri Color, preset Bambu PLA Silk.
            Spool(
                spool_id=12,
                material="PLA",
                subtype="Tri Color",
                slicer_filament_name="Bambu PLA Silk",
            ),
            Spool(spool_id=13, material="PETG", subtype="Silk"),
        ],
    )


def _plan(*spools: int | None) -> FilamentPlan:
    """One spool per slot, from slot 1; ``None`` leaves that slot without a choice."""
    return FilamentPlan(
        slots=[
            SlotChoice(slot_id=i, spool_id=s)
            for i, s in enumerate(spools, start=1)
            if s is not None
        ]
    )


def test_silk_sets_the_nozzle_temperature_as_bambus_guide_says() -> None:
    assert [(s.key, s.value, s.unit) for s in SILK.filament_settings] == [
        ("nozzle_temperature", "235", "°C"),
        ("nozzle_temperature_initial_layer", "235", "°C"),
    ]
    # Every one cited, and no longer only a note.
    assert all(setting.sources for setting in SILK.filament_settings)
    assert not any("235" in note.text for note in SILK.notes)


def test_only_the_slots_whose_spool_matches_get_the_temperature() -> None:
    overrides = slot_filament_overrides(["silk-pla"], _options(), _plan(10, 11, 12))
    assert overrides == {
        2: {"nozzle_temperature": "235", "nozzle_temperature_initial_layer": "235"},
        3: {"nozzle_temperature": "235", "nozzle_temperature_initial_layer": "235"},
    }


def test_no_profile_or_no_matching_spool_sets_nothing() -> None:
    assert slot_filament_overrides([], _options(), _plan(11)) == {}
    # PETG silk is not Silk PLA's; an empty slot matches nothing.
    assert slot_filament_overrides(["silk-pla"], _options(), _plan(13, None, 10)) == {}


def test_a_slot_the_plate_does_not_use_gets_nothing() -> None:
    options = _options().model_copy(update={"slots": [SlotNeed(slot_id=1)]})
    assert slot_filament_overrides(["silk-pla"], options, _plan(10, 11)) == {}


def test_bambuddys_slicer_says_the_temperature_is_not_applied() -> None:
    warning = filament_not_applied(SILK)
    assert warning.kind == "profile-not-applied"
    assert warning.message == (
        "Silk PLA profile's nozzle temperature and nozzle temperature (first layer) not "
        "applied: Bambuddy's slicer has no filament setting override, so set them in the "
        "spool's filament preset. The native slicer applies them."
    )
