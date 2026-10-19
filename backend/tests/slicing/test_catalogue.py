"""The preset catalogue from the image's BBL profiles (#3818 tier A Task 2): what a
native run offers and resolves, with no Bambuddy."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, SlotNeed, SpoolOption
from scadbuddy.bambuddy.models import PresetRef, SlotChoice
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices, resolve
from scadbuddy.bambuddy.trays import tray_spool_id
from scadbuddy.slicing import catalogue as native
from scadbuddy.slicing import profiles as profiles_module
from scadbuddy.slicing.catalogue import (
    FAILED_RETRY,
    catalogue_for,
    native_catalogue,
    native_ref,
    warm_catalogue,
)
from scadbuddy.slicing.component import COMPONENT as SLICING_COMPONENT
from scadbuddy.slicing.profiles import Kind, ProfileImageError, Profiles, flatten
from tests.bambuddy.conftest import recording
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


@pytest.mark.parametrize(
    ("ref", "said"),
    [
        (PresetRef(source="local", id="7"), "a Bambuddy local preset"),
        (PresetRef(source="cloud", id="PFUS1234"), "a Bambu Cloud user preset"),
        (PresetRef(source="orca_cloud", id="7"), "an OrcaSlicer cloud preset"),
    ],
)
def test_a_bambuddy_override_is_refused_on_native(
    profiles: Profiles, ref: PresetRef, said: str
) -> None:
    """D5: refused, saying what kind of preset it is; the remembered ref carries no name,
    and its bare id would mean nothing to a person."""
    choices = h2c_choices(filament_overrides={1: ref})
    resolved = resolve(
        tray_options("GFA00"), plan_for_tray(), choices, native_catalogue(profiles), {}
    )
    (error,) = resolved.errors
    assert error.slot_id == 1 and said in error.message
    assert "A native print uses Bambu Studio's own presets" in error.message
    assert ref.id not in error.message
    assert resolved.filament_presets == []


def test_a_standard_override_this_image_lacks_is_not_called_bambuddys(
    profiles: Profiles,
) -> None:
    """A standard preset of a newer Bambu Studio (Bambuddy's slicer may be one) is Bambu
    Studio's, not Bambuddy's: it is only missing from this image's version."""
    ref = PresetRef(source="standard", id="Bambu PLA Matte @BBL H2C")
    choices = h2c_choices(filament_overrides={1: ref})
    resolved = resolve(
        tray_options("GFA00"), plan_for_tray(), choices, native_catalogue(profiles), {}
    )
    (error,) = resolved.errors
    assert "not one of Bambu Studio's own presets in this version" in error.message
    assert "Bambuddy" not in error.message


@pytest.mark.parametrize(
    ("choices", "kind", "said"),
    [
        (
            PrintChoices(nozzles=[NozzleChoice(size="0.4")], process_name="My PLA fast"),
            "no-process",
            "'My PLA fast' is not a process Bambu Studio has for a 0.4 mm nozzle. A native"
            " print uses Bambu Studio's own presets: pick one under Advanced.",
        ),
        (
            PrintChoices(nozzles=[NozzleChoice(size="0.6")]),
            "no-preset",
            "Bambu Studio has no printer preset 'Bambu Lab H2C 0.6 nozzle'.",
        ),
        (
            PrintChoices(nozzles=[NozzleChoice(size="0.4"), NozzleChoice(size="0.6")]),
            "mixed-sizes",
            "The two nozzles are different sizes. The native slicer can't slice mixed"
            " nozzle sizes yet.",
        ),
    ],
)
def test_a_native_choice_error_names_bambu_studio_not_bambuddy(
    profiles: Profiles, choices: PrintChoices, kind: str, said: str
) -> None:
    resolved = resolve(
        tray_options("GFA00"), plan_for_tray(), choices, native_catalogue(profiles), {}
    )
    messages = [error.message for error in resolved.errors if error.kind == kind]
    assert said in messages
    assert not any("Bambuddy" in error.message for error in resolved.errors)


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


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "profiles"
    shutil.copytree(PROFILES, root)
    return root


def test_a_build_reads_each_profile_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path)
    reads: Counter[Path] = Counter()
    real = profiles_module._read

    def counting(path: Path) -> Any:
        reads[path] += 1
        return real(path)

    monkeypatch.setattr(profiles_module, "_read", counting)
    native_catalogue(Profiles(root))
    listed = sum(1 for _ in (root / "BBL").rglob("*.json"))
    assert len(reads) == listed + 1  # and BBL.json
    assert max(reads.values()) == 1


def _blocking_build(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """The build held until the event is set, as a multi-second build of the real tree is."""
    release = threading.Event()
    real = native._build

    def held(profiles: Profiles) -> Any:
        assert release.wait(10)
        return real(profiles)

    monkeypatch.setattr(native, "_build", held)
    return release


async def test_warming_starts_the_build_without_waiting_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _blocking_build(monkeypatch)
    profiles = Profiles(_tree(tmp_path))
    task = warm_catalogue(profiles)
    assert task is not None
    await asyncio.sleep(0.05)
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, 10)
    assert native._built[(profiles.root, profiles.version)] is native_catalogue(profiles)


def test_warming_without_a_tree_does_nothing() -> None:
    assert warm_catalogue(None) is None


async def test_a_failed_warm_is_logged_once_and_never_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = _tree(tmp_path)
    (root / "BBL" / "process" / "leaf.json").write_text("{not json")
    task = warm_catalogue(Profiles(root))
    assert task is not None
    with caplog.at_level(logging.WARNING, logger=native.__name__):
        await asyncio.wait_for(task, 10)
    assert [r.message for r in caplog.records] == ["could not build the native preset catalogue"]
    assert "{not json" not in caplog.text


async def test_the_api_start_does_not_wait_on_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SLICING component's run (entered in the lifespan) starts the warm and
    returns at once; leaving it stops waiting on the build."""
    release = _blocking_build(monkeypatch)
    slicing = SimpleNamespace(profiles=Profiles(_tree(tmp_path)))
    run = SLICING_COMPONENT.run
    assert run is not None
    async with asyncio.timeout(1):
        async with run(cast(Any, slicing)):
            pass
    release.set()


def test_a_failed_build_is_answered_from_memory_for_a_while(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path)
    builds: list[Path] = []

    def broken(profiles: Profiles) -> Any:
        builds.append(profiles.root)
        raise ProfileImageError("the profile file BBL.json cannot be read")

    clock = [1000.0]
    monkeypatch.setattr(native, "_build", broken)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    for _ in range(2):
        with pytest.raises(ProfileImageError):
            native_catalogue(Profiles(root))
    assert len(builds) == 1
    clock[0] += FAILED_RETRY + 1
    with pytest.raises(ProfileImageError):
        native_catalogue(Profiles(root))
    assert len(builds) == 2


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


#: Every H2C printer and nozzle Bambu Studio ships a preset for.
H2C_PRINTERS = [f"Bambu Lab H2C {size} nozzle" for size in ("0.2", "0.4", "0.6", "0.8")]


@pytest.mark.requires_slicer_profiles
def test_a_trays_filament_id_names_one_preset_per_h2c_printer() -> None:
    """``_by_filament_id`` breaks a tie by list order, so it is right only while no
    ``filament_id`` names two presets that fit one H2C printer and nozzle (measured on
    v02.08.02.61: none does). A Bambu Studio bump that adds one fails here."""
    catalogue = native_catalogue(Profiles(SLICER_PROFILES_DIR))
    recorded = {
        tray["tray_info_idx"]
        for name in ("printer-status.json", "printer-status-rack.json", "printer-status-fts.json")
        for tray in _trays(recording(name))
        if tray.get("tray_info_idx")
    }
    bbl = {idx for idx in recorded if any(row.filament_id == idx for row in catalogue.filament)}
    assert bbl == {"GFA00", "GFA05", "GFG00", "GFL99", "GFS05"}
    for printer in H2C_PRINTERS:
        ids = Counter(
            row.filament_id for row in catalogue.filament if printer in row.compatible_printers
        )
        assert ids and not [idx for idx, n in ids.items() if n > 1], printer
        for idx in bbl:
            assert ids[idx] == 1, (printer, idx)


def _trays(doc: Any) -> list[dict[str, Any]]:
    """Every dict under ``doc`` that carries a ``tray_info_idx``."""
    found: list[dict[str, Any]] = []
    if isinstance(doc, dict):
        if "tray_info_idx" in doc:
            found.append(doc)
        for value in doc.values():
            found.extend(_trays(value))
    elif isinstance(doc, list):
        for value in doc:
            found.extend(_trays(value))
    return found


@pytest.mark.requires_slicer_profiles
def test_each_presets_own_setting_id_is_its_flattened_one() -> None:
    """``Profiles.find`` keys a cloud ref on each file's own ``setting_id``; the catalogue
    keys it on the flattened one. They agree only while no preset inherits one."""
    profiles = Profiles(SLICER_PROFILES_DIR)
    catalogue = native_catalogue(profiles)
    kinds: list[tuple[Kind, str]] = [
        ("machine", "printer"),
        ("process", "process"),
        ("filament", "filament"),
    ]
    for kind, field in kinds:
        docs = profiles.documents(kind)
        ids = (catalogue.native_setting_ids or {})[field]
        for row in getattr(catalogue, field):
            own = docs[row.name].get("setting_id")
            assert own and ids.get(own) == row.name, row.name
            assert profiles.find(kind, own) == row.name


@pytest.mark.requires_slicer_profiles
def test_the_native_catalogue_matches_bambuddys_for_the_h2c() -> None:
    """Measured against ``slicer-presets-h2c.json`` (Bambuddy's catalogue for the H2C):
    every standard and cloud process and filament row is in the native catalogue with the
    same name and ``compatible_printers``, each cloud id maps to its name, and for the
    H2C 0.4 the two offer the same presets."""
    catalogue = native_catalogue(Profiles(SLICER_PROFILES_DIR))
    bambuddy = recording("slicer-presets-h2c.json")
    for field in ("process", "filament"):
        native_rows = {row.name: row for row in getattr(catalogue, field)}
        ids = (catalogue.native_setting_ids or {})[field]
        rows = [*bambuddy["standard"][field], *bambuddy["cloud"][field]]
        for row in rows:
            assert row["name"] in native_rows, row["name"]
            assert sorted(row["compatible_printers"] or []) == sorted(
                native_rows[row["name"]].compatible_printers
            ), row["name"]
        for row in bambuddy["cloud"][field]:
            assert ids.get(row["id"]) == row["name"], row["id"]
        h2c = "Bambu Lab H2C 0.4 nozzle"
        theirs = {row["name"] for row in rows if h2c in (row["compatible_printers"] or [])}
        ours = {name for name, row in native_rows.items() if h2c in row.compatible_printers}
        assert theirs == ours, field
    for row in bambuddy["cloud"]["printer"]:
        assert catalogue.native_ref(PresetRef(source="cloud", id=row["id"])) == PresetRef(
            source="standard", id=row["name"]
        )
