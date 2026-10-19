"""The preset catalogue from the image's BBL profiles (#3818 tier A Task 2): what a
native run offers and resolves, with no Bambuddy."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from scadbuddy.bambuddy.catalogue import catalogue_for
from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, SlotNeed, SpoolOption
from scadbuddy.bambuddy.models import PresetRef, SlotChoice
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices, resolve
from scadbuddy.bambuddy.trays import tray_spool_id
from scadbuddy.slicing.catalogue import native_catalogue, native_ref
from scadbuddy.slicing.profiles import Profiles, flatten
from tests.conftest import SLICER_PROFILES_DIR
from tests.slicing.conftest import PROFILES

H2C = "Bambu Lab H2C 0.4 nozzle"
TRAY = tray_spool_id(0, 0)


def tray_options(tray_info_idx: str | None, material: str = "PLA") -> FilamentOptions:
    """One slot, and one tray chosen for it: what a native run's trays offer."""
    return FilamentOptions(
        library_file_id=None,
        slots=[SlotNeed(slot_id=1, colour="#FFFFFF")],
        spools=[
            SpoolOption(
                spool_id=TRAY,
                material=material,
                colour="#FFFFFF",
                slicer_filament=tray_info_idx,
                tray_only=True,
            )
        ],
    )


def plan_for_tray() -> FilamentPlan:
    return FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=TRAY)])


def h2c_choices(**kwargs: object) -> PrintChoices:
    return PrintChoices(nozzles=[NozzleChoice(size="0.4")], **kwargs)  # type: ignore[arg-type]


def test_the_native_catalogue_offers_only_instantiable_presets(profiles: Profiles) -> None:
    catalogue = native_catalogue(profiles)
    names = {c.name for c in catalogue.filament}
    assert "Bambu PLA Basic @BBL H2C" in names and "Test PLA" in names
    assert "Bambu PLA Basic @base" not in names and "fdm_filament_root" not in names
    assert {c.name for c in catalogue.process} >= {"leaf", "0.20mm Standard @BBL H2C"}
    assert "mid" not in {c.name for c in catalogue.process}
    assert {c.name for c in catalogue.printer} == {"Test Printer 0.4 nozzle", H2C}


def test_a_preset_the_native_slicer_refuses_is_not_offered(profiles: Profiles) -> None:
    """An inherits loop, a missing parent, ``includes`` and a short per-filament list
    are refused by :func:`flatten`, so the dialog never offers what cannot slice."""
    catalogue = native_catalogue(profiles)
    processes = {c.name for c in catalogue.process}
    assert not processes & {"loop_a", "loop_b", "orphan", "with_includes"}
    assert "Test Short" not in {c.name for c in catalogue.filament}


def test_a_standard_ref_is_the_preset_name(profiles: Profiles) -> None:
    row = next(
        c for c in native_catalogue(profiles).process if c.name == "0.20mm Standard @BBL H2C"
    )
    assert row.ref == PresetRef(source="standard", id="0.20mm Standard @BBL H2C")
    assert row.compatible_printers == [H2C]


def test_a_filament_row_carries_its_inherited_id_type_and_colour(profiles: Profiles) -> None:
    row = next(
        c for c in native_catalogue(profiles).filament if c.name == "Bambu PLA Basic @BBL H2C"
    )
    assert row.filament_id == "GFA00"
    assert row.filament_type == "PLA"
    assert row.filament_colour == "#FFFFFF"


def test_a_cloud_ref_maps_to_its_standard_ref(profiles: Profiles) -> None:
    ref = native_ref(profiles, PresetRef(source="cloud", id="GFSA00_99"))
    assert ref == PresetRef(source="standard", id="Bambu PLA Basic @BBL H2C")


def test_a_standard_ref_in_the_tree_is_kept(profiles: Profiles) -> None:
    ref = PresetRef(source="standard", id="Test PETG")
    assert native_ref(profiles, ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        PresetRef(source="local", id="7"),
        PresetRef(source="orca_cloud", id="Test PLA"),
        # A cloud user preset: a setting_id the tree does not hold.
        PresetRef(source="cloud", id="PFUS1234"),
        PresetRef(source="standard", id="0.20mm Standard @BBL X9Z"),
        # In the tree, but a base preset: never offered, so never resolved.
        PresetRef(source="standard", id="Bambu PLA Basic @base"),
    ],
)
def test_a_ref_outside_the_offered_tree_has_no_native_ref(
    profiles: Profiles, ref: PresetRef
) -> None:
    assert native_ref(profiles, ref) is None


def test_a_tray_resolves_its_preset_by_filament_id(profiles: Profiles) -> None:
    options = tray_options("GFA00")
    resolved = resolve(options, plan_for_tray(), h2c_choices(), native_catalogue(profiles), {})
    assert resolved.errors == []
    assert resolved.filament_presets == [
        PresetRef(source="standard", id="Bambu PLA Basic @BBL H2C")
    ]


def test_a_trays_filament_id_resolves_only_a_preset_that_fits_the_printer(
    profiles: Profiles,
) -> None:
    """``GFA00`` is the id of a 0.4 H2C and an X1C preset; on a 0.2 nozzle neither fits,
    so the slot falls through to the name fallbacks and, with none, has no preset."""
    choices = PrintChoices(nozzles=[NozzleChoice(size="0.2")])
    resolved = resolve(
        tray_options("GFA00"), plan_for_tray(), choices, native_catalogue(profiles), {}
    )
    assert [error.kind for error in resolved.errors if error.slot_id == 1] == ["no-preset"]
    assert resolved.filament_presets == []


def test_an_override_by_cloud_ref_resolves_to_its_standard_ref(profiles: Profiles) -> None:
    choices = h2c_choices(filament_overrides={1: PresetRef(source="cloud", id="GFSA00_99")})
    resolved = resolve(tray_options(None), plan_for_tray(), choices, native_catalogue(profiles), {})
    assert resolved.errors == []
    assert resolved.filament_presets == [
        PresetRef(source="standard", id="Bambu PLA Basic @BBL H2C")
    ]


@pytest.mark.parametrize("source", ["local", "orca_cloud", "cloud"])
def test_a_bambuddy_override_is_refused_on_native(profiles: Profiles, source: str) -> None:
    ref = PresetRef(source=source, id="7")  # type: ignore[arg-type]
    choices = h2c_choices(filament_overrides={1: ref})
    resolved = resolve(
        tray_options("GFA00"), plan_for_tray(), choices, native_catalogue(profiles), {}
    )
    assert len(resolved.errors) == 1
    message = resolved.errors[0].message
    assert f"Bambuddy's ({source})" in message
    assert "Bambu Studio's own presets" in message
    assert resolved.filament_presets == []


async def test_catalogue_for_native_needs_no_client(profiles: Profiles) -> None:
    catalogue = await catalogue_for(None, native=profiles)
    assert {c.name for c in catalogue.process} >= {"leaf"}


async def test_catalogue_for_without_either_is_refused() -> None:
    with pytest.raises(ValueError, match="Bambuddy client"):
        await catalogue_for(None, native=None)


def test_the_catalogue_is_built_once_per_tree_and_version(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    shutil.copytree(PROFILES, root)
    first = native_catalogue(Profiles(root))
    assert native_catalogue(Profiles(root)) is first
    (root / "VERSION").write_text("99.99.99.99\n")
    assert native_catalogue(Profiles(root)) is not first


@pytest.mark.requires_slicer_profiles
def test_the_images_tree_builds() -> None:
    """Every instantiable preset of the image's tree is offered (none is refused by
    :func:`flatten` in v02.08.02.61), with the fields read from its flattened whole."""
    profiles = Profiles(SLICER_PROFILES_DIR)
    started = time.monotonic()
    catalogue = native_catalogue(profiles)
    elapsed = time.monotonic() - started
    vendor = json.loads((SLICER_PROFILES_DIR / "BBL.json").read_text())
    for kind, rows in (
        ("machine", catalogue.printer),
        ("process", catalogue.process),
        ("filament", catalogue.filament),
    ):
        listed = [
            entry["name"]
            for entry in vendor[f"{kind}_list"]
            if json.loads((SLICER_PROFILES_DIR / "BBL" / entry["sub_path"]).read_text()).get(
                "instantiation"
            )
            != "false"
        ]
        assert rows and sorted(row.name for row in rows) == sorted(listed), kind
    basic = next(c for c in catalogue.filament if c.name == "Bambu PLA Basic @BBL H2C")
    flat = flatten(basic.name, lambda name: profiles._load("filament", name))
    assert basic.filament_id == flat["filament_id"] == "GFA00"
    assert basic.compatible_printers == flat["compatible_printers"]
    assert native_ref(profiles, PresetRef(source="cloud", id="GFSB00_22")) == PresetRef(
        source="standard", id="Bambu ABS @BBL H2C"
    )
    print(f"native_catalogue: {elapsed:.2f} s")
