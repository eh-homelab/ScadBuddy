"""Bambuddy's local presets on a native run, from the kept copy (#4645, hot-path plan
2026-10-18 D5): offered, resolved, valued and sliced with no Bambuddy call."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import cast

import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, SlotNeed, SpoolOption
from scadbuddy.bambuddy.models import PresetRef, SlotChoice, SpoolFilamentPreset
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices, resolve
from scadbuddy.bambuddy.slot_presets import slot_preset_name
from scadbuddy.bambuddy.trays import tray_spool_id
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.slicing import catalogue as catalogue_module
from scadbuddy.slicing.catalogue import catalogue_for, native_catalogue
from scadbuddy.slicing.held_presets import HeldPresetStore, held_row, refresh_held
from scadbuddy.slicing.presets import build_presets, native_preset_key, needs_bambuddy
from scadbuddy.slicing.profiles import ProfileError, Profiles
from scadbuddy.slicing.values import native_values
from scadbuddy.spools.models import SpoolSnapshot
from tests.slicing.conftest import PROFILES, MemoryHeld, local_preset

BASE = "https://bambuddy.test"
H2C = "Bambu Lab H2C 0.4 nozzle"
TRAY = tray_spool_id(0, 0)

#: A user's filament as Bambuddy keeps it: imported over a preset of the image's tree.
PLA = local_preset(
    7,
    "Inland PLA Black (3DFP OyJSo2eL7) @H2C",
    "filament",
    {
        "type": "filament",
        "name": "Inland PLA Black (3DFP OyJSo2eL7) @H2C",
        "from": "User",
        "inherits": "Bambu PLA Basic @BBL H2C",
        "filament_vendor": ["Inland"],
        "nozzle_temperature": ["215"],
    },
    filament_type="PLA",
    compatible_printers=json.dumps([H2C]),
    default_filament_colour=json.dumps(["#000000"]),
    updated_at="2026-10-22T10:00:00",
)
#: A user's process, over the tree's ``leaf``.
FINE = local_preset(
    9,
    "My Fine @H2C",
    "process",
    {"type": "process", "name": "My Fine @H2C", "inherits": "leaf", "layer_height": "0.08"},
    compatible_printers=json.dumps([H2C]),
)
#: Inherits a preset this image's Bambu Studio does not ship.
ORPHAN = local_preset(
    11,
    "Orphan PLA @H2D",
    "filament",
    {"type": "filament", "name": "Orphan PLA @H2D", "inherits": "Generic PLA @BBL H2D"},
    compatible_printers=json.dumps([H2C]),
)


@pytest.fixture
def held() -> MemoryHeld:
    return MemoryHeld(PLA, FINE, ORPHAN)


@pytest.fixture
def kept(held: MemoryHeld) -> Profiles:
    return Profiles(PROFILES, held=held)


@pytest.fixture
async def client() -> AsyncIterator[BambuddyClient]:
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        yield client


def local(plan: SlicePlan, **slots: PresetRef | list[PresetRef]) -> SlicePlan:
    return plan.model_copy(update=slots)


# -- the slice's files ----------------------------------------------------------------


async def test_a_kept_local_filament_slices_with_no_bambuddy(
    kept: Profiles, plan: SlicePlan
) -> None:
    files = await build_presets(
        local(plan, filament_presets=[PresetRef(source="local", id="7")]), kept, None
    )
    flat = json.loads(files.filaments[0])
    assert flat["nozzle_temperature"] == ["215"] and flat["filament_vendor"] == ["Inland"]
    # Flattened over the tree's parent: what the user's document leaves out is the parent's.
    assert flat["compatible_printers"] == [H2C] and flat["setting_id"] == "GFSA00_99"
    assert flat["name"] == PLA.name and "inherits" not in flat
    assert set(files.from_bambuddy) == {"filament_1"}


@respx.mock
async def test_the_kept_copy_is_read_before_bambuddy(
    kept: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    """With a client too, a kept preset is not asked of Bambuddy (respx refuses any
    route not mocked here), so a slice's start and its prepare build it alike."""
    files = await build_presets(
        local(
            plan,
            printer_preset=PresetRef(source="standard", id="Test Printer 0.4 nozzle"),
            filament_presets=[PresetRef(source="local", id="7")],
        ),
        kept,
        client,
    )
    assert json.loads(files.filaments[0])["nozzle_temperature"] == ["215"]


async def test_a_local_preset_not_kept_still_needs_bambuddy(plan: SlicePlan) -> None:
    profiles = Profiles(PROFILES, held=MemoryHeld())
    with pytest.raises(ProfileError, match=r"local filament preset 8.*needs Bambuddy, and none"):
        await build_presets(
            local(plan, filament_presets=[PresetRef(source="local", id="8")]), profiles, None
        )


async def test_a_kept_local_process_is_flattened_over_the_tree(
    kept: Profiles, plan: SlicePlan
) -> None:
    """Over the image's own tree, as Bambu Studio loads a user preset, not Bambuddy's
    flattened values: a native run reads no Bambuddy."""
    files = await build_presets(
        local(plan, process_preset=PresetRef(source="local", id="9")), kept, None
    )
    process = json.loads(files.process)
    assert process["layer_height"] == "0.08" and process["inner_wall_speed"] == ["300"]
    assert process["curr_bed_type"] == "Textured PEI Plate"
    assert set(files.from_bambuddy) == {"process"}


async def test_a_kept_preset_whose_parent_the_tree_lacks_is_refused(
    kept: Profiles, plan: SlicePlan
) -> None:
    with pytest.raises(ProfileError, match=r"inherits Generic PLA @BBL H2D: no such preset"):
        await build_presets(
            local(plan, filament_presets=[PresetRef(source="local", id="11")]), kept, None
        )


async def test_a_kept_preset_of_another_slot_is_refused(kept: Profiles, plan: SlicePlan) -> None:
    with pytest.raises(ProfileError, match=r"local preset 9 is a process preset"):
        await build_presets(
            local(plan, filament_presets=[PresetRef(source="local", id="9")]), kept, None
        )


async def test_only_a_local_preset_not_kept_needs_bambuddy(kept: Profiles, plan: SlicePlan) -> None:
    keptref = local(
        plan,
        process_preset=PresetRef(source="local", id="9"),
        filament_presets=[PresetRef(source="local", id="7")],
    )
    assert not await needs_bambuddy(keptref, kept)
    missing = local(plan, filament_presets=[PresetRef(source="local", id="8")])
    assert await needs_bambuddy(missing, kept)


async def test_an_edited_kept_preset_slices_again(
    held: MemoryHeld, kept: Profiles, plan: SlicePlan
) -> None:
    """The key carries what the copy held: an edit in Bambuddy, once kept, is another
    slice under the same id."""
    pla = local(plan, filament_presets=[PresetRef(source="local", id="7")])
    before = await build_presets(pla, kept, None)
    setting = {**PLA.setting, "nozzle_temperature": ["220"]}
    held.docs[7] = PLA.model_copy(update={"setting": setting})
    after = await build_presets(pla, kept, None)
    version = kept.version
    assert native_preset_key(pla, 1, version, before.from_bambuddy) != native_preset_key(
        pla, 1, version, after.from_bambuddy
    )


# -- the catalogue ----------------------------------------------------------------------


async def test_the_native_catalogue_offers_the_kept_local_presets(kept: Profiles) -> None:
    catalogue = await catalogue_for(None, native=kept)
    (pla,) = [row for row in catalogue.filament if row.ref.source == "local"]
    assert pla.ref == PresetRef(source="local", id="7") and pla.name == PLA.name
    assert pla.compatible_printers == [H2C] and pla.filament_type == "PLA"
    assert pla.filament_colour == "#000000"
    assert [row.ref for row in catalogue.process if row.ref.source == "local"] == [
        PresetRef(source="local", id="9")
    ]
    # The image's own rows are all still there.
    assert {row.name for row in native_catalogue(kept).filament} <= {
        row.name for row in catalogue.filament
    }


async def test_a_kept_preset_the_native_slicer_cannot_flatten_is_not_offered(
    kept: Profiles,
) -> None:
    catalogue = await catalogue_for(None, native=kept)
    assert ORPHAN.name not in {row.name for row in catalogue.filament}


async def test_a_kept_preset_is_flattened_once_per_version(
    held: MemoryHeld, kept: Profiles
) -> None:
    """A dialog opens the catalogue each time: the documents are read only for a
    preset not checked before, or edited since."""
    await catalogue_for(None, native=kept)
    held.asked.clear()
    await catalogue_for(None, native=kept)
    assert held.asked == []
    held.docs[7] = PLA.model_copy(update={"setting": {**PLA.setting, "filament_vendor": ["X"]}})
    await catalogue_for(None, native=kept)
    assert held.asked == [[7]]


async def test_a_slot_preset_is_not_offered() -> None:
    """ScadBuddy's own per-slot copies are never offered beside the preset they copy, as
    on Bambuddy's catalogue (#3962)."""
    slot = PLA.model_copy(update={"id": 12, "name": slot_preset_name(PLA.name, 2)})
    catalogue = await catalogue_for(None, native=Profiles(PROFILES, held=MemoryHeld(slot)))
    assert all(row.ref.source != "local" for row in catalogue.filament)


async def test_without_a_kept_copy_the_catalogue_is_the_images(profiles: Profiles) -> None:
    assert await catalogue_for(None, native=profiles) is native_catalogue(profiles)


async def test_a_kept_ref_is_its_own_native_ref(kept: Profiles) -> None:
    catalogue = await catalogue_for(None, native=kept)
    ref = PresetRef(source="local", id="7")
    assert catalogue.native_ref(ref, "filament") == ref and catalogue.native_ref(ref) == ref
    assert catalogue.native_ref(ref, "process") is None
    assert catalogue.native_ref(PresetRef(source="local", id="11"), "filament") is None


# -- the resolver ---------------------------------------------------------------------


def h2c_choices(**kwargs: object) -> PrintChoices:
    return PrintChoices(nozzles=[NozzleChoice(size="0.4")], **kwargs)  # type: ignore[arg-type]


def tray(records: dict[str, SpoolSnapshot] | None = None) -> FilamentOptions:
    return FilamentOptions(
        library_file_id=None,
        slots=[SlotNeed(slot_id=1, colour="#FFFFFF")],
        spools=[
            SpoolOption(
                spool_id=TRAY,
                material="PLA",
                colour="#000000",
                tray_only=True,
                records=records or {},
            )
        ],
    )


def tray_plan() -> FilamentPlan:
    return FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=TRAY)])


async def test_a_kept_local_override_resolves_on_native(kept: Profiles) -> None:
    ref = PresetRef(source="local", id="7")
    resolved = resolve(
        tray(),
        tray_plan(),
        h2c_choices(filament_overrides={1: ref}),
        await catalogue_for(None, native=kept),
        {},
    )
    assert resolved.errors == [] and resolved.filament_presets == [ref]


async def test_a_local_override_not_kept_is_refused_saying_so(kept: Profiles) -> None:
    ref = PresetRef(source="local", id="8")
    resolved = resolve(
        tray(),
        tray_plan(),
        h2c_choices(filament_overrides={1: ref}),
        await catalogue_for(None, native=kept),
        {},
    )
    (error,) = resolved.errors
    assert "a Bambuddy local preset ScadBuddy has no copy of" in error.message


async def test_a_providers_record_naming_a_kept_preset_resolves(kept: Profiles) -> None:
    """Bambuddy's spool names its imported preset by its local id: on a native run that
    is the kept preset."""
    records = {"bambuddy": SpoolSnapshot(record_id="16", preset="7")}
    resolved = resolve(
        tray(records), tray_plan(), h2c_choices(), await catalogue_for(None, native=kept), {}
    )
    assert resolved.errors == []
    assert resolved.filament_presets == [PresetRef(source="local", id="7")]


async def test_a_providers_record_naming_a_kept_preset_for_any_printer_resolves() -> None:
    """An empty ``compatible_printers`` declares no restriction (`PresetChoice`), so a
    kept preset that leaves it empty matches a provider's record too (#4645 review)."""
    anywhere = PLA.model_copy(update={"compatible_printers": None})
    profiles = Profiles(PROFILES, held=MemoryHeld(anywhere))
    records = {"bambuddy": SpoolSnapshot(record_id="16", preset="7")}
    resolved = resolve(
        tray(records), tray_plan(), h2c_choices(), await catalogue_for(None, native=profiles), {}
    )
    assert resolved.errors == []
    assert resolved.filament_presets == [PresetRef(source="local", id="7")]


async def test_an_edit_replaces_the_flatten_answer_it_had(held: MemoryHeld, kept: Profiles) -> None:
    """One answer per kept preset, the latest edit's (#4645 review): an edit never adds
    an entry that lives for the process's life."""
    await catalogue_for(None, native=kept)
    before = len(catalogue_module._flattens)
    for n in range(3):
        setting = {**PLA.setting, "filament_vendor": [f"V{n}"]}
        held.docs[7] = PLA.model_copy(update={"setting": setting})
        await catalogue_for(None, native=kept)
    assert len(catalogue_module._flattens) == before


async def test_a_spools_own_kept_preset_resolves_on_native(kept: Profiles) -> None:
    options = FilamentOptions(
        library_file_id=None,
        slots=[SlotNeed(slot_id=1, colour="#FFFFFF")],
        spools=[SpoolOption(spool_id=16, material="PLA", colour="#000000")],
    )
    own = {
        16: [SpoolFilamentPreset(printer_model="H2C", nozzle_diameter="0.4", slicer_filament="7")]
    }
    resolved = resolve(
        options,
        FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=16)]),
        h2c_choices(),
        await catalogue_for(None, native=kept),
        own,
    )
    assert resolved.errors == []
    assert resolved.filament_presets == [PresetRef(source="local", id="7")]


# -- the values -------------------------------------------------------------------------


async def test_a_kept_local_process_has_values(kept: Profiles) -> None:
    values = await native_values(kept)(PresetRef(source="local", id="9"))
    assert values.resolved and values.values["layer_height"] == "0.08"
    assert values.values["inner_wall_speed"] == ["300"]


async def test_a_local_process_not_kept_is_unresolved(kept: Profiles) -> None:
    assert not (await native_values(kept)(PresetRef(source="local", id="8"))).resolved


async def test_without_bambuddy_the_refresh_keeps_the_copy_as_it_is() -> None:
    """As a spool provider's rows stay when it is not configured: the store is never
    touched (a ``None`` store would raise if it were)."""
    outcome = await refresh_held(cast(HeldPresetStore, None), StoredSettings)
    assert outcome == "skipped"


def test_a_held_rows_digest_is_its_settings() -> None:
    edited = PLA.model_copy(update={"setting": {**PLA.setting, "nozzle_temperature": ["1"]}})
    assert held_row(PLA).sha256 != held_row(edited).sha256
    assert held_row(PLA).preset.name == PLA.name
