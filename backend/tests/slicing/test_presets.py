"""Presets as files for the native slicer (#3822, plan 2026-10-16 Task 8)."""

import hashlib
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.dispatch import SlicePlan, slice_request
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.slicing.presets import (
    apply_overrides,
    build_presets,
    native_preset_key,
    needs_bambuddy,
)
from scadbuddy.slicing.profiles import Flat, ProfileError, Profiles, flatten
from tests.conftest import SLICER_PROFILES_DIR
from tests.slicing.conftest import PROFILES

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"


@pytest.fixture
async def client() -> AsyncIterator[BambuddyClient]:
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        yield client


def test_flatten_child_wins_and_drops_inherits(profiles: Profiles) -> None:
    out = profiles.process("leaf")  # leaf -> mid -> fdm_process_root
    assert out["layer_height"] == "0.16" and out["wall_loops"] == "3" and "inherits" not in out
    assert out["sparse_infill_density"] == "15%" and out["name"] == "leaf"


def test_a_childs_list_replaces_the_parents_whole(profiles: Profiles) -> None:
    # Bambu Studio applies a child's option over its parent's as one value
    # (`config.apply(config_src)`), so a shorter list is not filled from the parent's.
    assert profiles.process("short_list")["outer_wall_speed"] == ["150"]


def test_flatten_refuses_a_loop(profiles: Profiles) -> None:
    with pytest.raises(ProfileError, match=r"loop.*loop_a"):
        profiles.process("loop_a")


def test_flatten_refuses_a_missing_parent(profiles: Profiles) -> None:
    with pytest.raises(ProfileError, match=r"orphan.*no such"):
        profiles.process("orphan")


def test_an_include_applies_between_the_parent_and_the_child(profiles: Profiles) -> None:
    """#4714: Bambu Studio's key is ``include``. The H2C takes its whole G-code from
    the presets it names, so dropping them left fdm_machine_common's generic start,
    which loads no filament, and every plate's slice_info named filament 1."""
    out = profiles.machine("Test Printer with includes")
    assert out["machine_start_gcode"].startswith(";===== machine: Test Printer")
    assert out["change_filament_gcode"] == "M620 S[next_extruder]A\nT[next_extruder]\n"
    # The child's own option wins over the included one, which wins over the parent's.
    assert out["machine_end_gcode"] == ";child end\n"
    assert out["name"] == "Test Printer with includes" and out["instantiation"] == "true"
    assert out["setting_id"] == "GM_INCLUDES"
    assert "include" not in out and "inherits" not in out


def test_flatten_refuses_a_missing_include(profiles: Profiles) -> None:
    # Bambu Studio only logs "can not find include" and slices without its G-code.
    with pytest.raises(ProfileError, match=r"with_includes includes no_such_process"):
        profiles.process("with_includes")


def test_flatten_refuses_an_include_with_a_parent_of_its_own() -> None:
    # Which of its options differ from the defaults cannot be told from its parent's.
    docs: dict[str, dict[str, Any]] = {
        "root": {"name": "root", "machine_start_gcode": ";root"},
        "template": {"name": "template", "inherits": "root", "machine_start_gcode": ";t"},
        "printer": {"name": "printer", "inherits": "root", "include": ["template"]},
    }
    with pytest.raises(ProfileError, match=r"printer.*template, names inherits"):
        flatten("printer", docs.get)


def test_flatten_refuses_a_value_that_is_not_text() -> None:
    docs = {"bad": {"name": "bad", "layer_height": 0.2}}
    with pytest.raises(ProfileError, match=r"bad.*layer_height"):
        flatten("bad", docs.get)


def test_a_missing_preset_is_named(profiles: Profiles) -> None:
    with pytest.raises(ProfileError, match="no process preset nope"):
        profiles.process("nope")


def test_a_ref_resolves_by_setting_id(profiles: Profiles) -> None:
    assert profiles.process("GP_LEAF") == profiles.process("leaf")


def test_kinds_do_not_cross(profiles: Profiles) -> None:
    with pytest.raises(ProfileError, match="no machine preset leaf"):
        profiles.machine("leaf")


def test_version_is_the_images_not_the_profiles(profiles: Profiles) -> None:
    assert profiles.version == "02.08.02.61" and profiles.profile_version == "02.08.00.05"


def test_installed_is_none_without_profiles(tmp_path: Path) -> None:
    assert Profiles.installed(tmp_path) is None


def test_installed_reads_a_tree(profiles: Profiles) -> None:
    installed = Profiles.installed(profiles.root)
    assert installed is not None and installed.process("leaf") == profiles.process("leaf")


def test_apply_overrides_per_extruder() -> None:
    speeds: Flat = {"outer_wall_speed": ["200", "200"]}
    assert apply_overrides(speeds, {"outer_wall_speed": ["150", "200"]}) == {
        "outer_wall_speed": ["150", "200"]
    }
    assert apply_overrides(speeds, {"outer_wall_speed": "150"}) == {
        "outer_wall_speed": ["150", "150"]
    }
    assert apply_overrides({"wall_loops": "2"}, {"wall_loops": "4"}) == {"wall_loops": "4"}
    assert apply_overrides({}, {"brim_width": "5"}) == {"brim_width": "5"}
    assert speeds == {"outer_wall_speed": ["200", "200"]}  # not changed in place


def test_apply_overrides_keeps_positions_a_short_list_does_not_name() -> None:
    assert apply_overrides({"v": ["1", "2", "3"]}, {"v": ["9"]}) == {"v": ["9", "2", "3"]}


async def test_build_presets_gives_one_file_per_slot(profiles: Profiles, plan: SlicePlan) -> None:
    files = await build_presets(plan, profiles, None)
    assert len(files.filaments) == 2 and json.loads(files.process)["layer_height"] == "0.16"
    assert json.loads(files.machine)["printable_height"] == "250"
    first, second = (json.loads(f) for f in files.filaments)
    assert first["name"] == "Test PLA" and first["nozzle_temperature"] == ["210"]
    assert first["filament_id"] == "GFL_TEST"  # from its @base parent
    assert second["filament_type"] == ["PETG"]


async def test_build_presets_writes_the_plate_type(profiles: Profiles, plan: SlicePlan) -> None:
    # The v02.08.02.61 CLI has no plate-type flag: a plate without its own takes the
    # process config's `curr_bed_type` (BambuStudio.cpp, the plate loop's "bed type").
    files = await build_presets(plan, profiles, None)
    assert json.loads(files.process)["curr_bed_type"] == "Textured PEI Plate"


async def test_build_presets_applies_the_overrides(profiles: Profiles, plan: SlicePlan) -> None:
    overridden = plan.model_copy(
        update={"process_overrides": {"wall_loops": "5", "outer_wall_speed": "120"}}
    )
    process = json.loads((await build_presets(overridden, profiles, None)).process)
    assert process["wall_loops"] == "5" and process["outer_wall_speed"] == ["120", "120"]


async def test_build_presets_does_not_write_the_colours(
    profiles: Profiles, plan: SlicePlan
) -> None:
    files = await build_presets(plan, profiles, None)
    assert all("#FF0000" not in f.decode() for f in files.filaments)


async def test_a_local_filament_needs_bambuddy(profiles: Profiles, plan: SlicePlan) -> None:
    local = plan.model_copy(update={"filament_presets": [PresetRef(source="local", id="7")]})
    with pytest.raises(ProfileError, match=r"local filament preset 7.*needs Bambuddy, and none"):
        await build_presets(local, profiles, None)


async def test_a_local_printer_needs_bambuddy(profiles: Profiles, plan: SlicePlan) -> None:
    local = plan.model_copy(update={"printer_preset": PresetRef(source="local", id="3")})
    with pytest.raises(ProfileError, match=r"local printer preset 3.*needs Bambuddy, and none"):
        await build_presets(local, profiles, None)


#: A user's filament preset as Bambuddy's ``GET /local-presets/7`` answers it (#4233): the
#: row's columns beside ``setting``, the imported document merged over its OrcaSlicer base
#: with ``inherits`` kept, here naming a preset of the image's tree.
LOCAL_PLA: dict[str, Any] = {
    "id": 7,
    "name": "Inland PLA Black (3DFP OyJSo2eL7) @H2C",
    "preset_type": "filament",
    "source": "orcaslicer",
    "inherits": "Test PLA",
    "setting": {
        "type": "filament",
        "name": "Inland PLA Black (3DFP OyJSo2eL7) @H2C",
        "from": "User",
        "inherits": "Test PLA",
        "filament_vendor": ["Inland"],
        "nozzle_temperature": ["215"],
    },
}


@respx.mock
async def test_a_local_filament_takes_bambuddys_document_over_the_trees_parent(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    """The document is flattened over the tree's preset its ``inherits`` names, child
    over parent, as Bambu Studio loads a user preset; the row names the preset, so the
    catalogue is not read (respx refuses any route not mocked here)."""
    respx.get(f"{API}/local-presets/7").respond(200, json=LOCAL_PLA)
    local = plan.model_copy(update={"filament_presets": [PresetRef(source="local", id="7")]})
    files = await build_presets(local, profiles, client)
    flat = json.loads(files.filaments[0])
    assert flat["nozzle_temperature"] == ["215"] and flat["filament_vendor"] == ["Inland"]
    assert flat["filament_id"] == "GFL_TEST" and flat["filament_type"] == ["PLA"]
    assert flat["name"] == LOCAL_PLA["name"] and "inherits" not in flat
    assert files.from_bambuddy == {"filament_1": hashlib.sha256(files.filaments[0]).hexdigest()}


@respx.mock
async def test_a_local_printer_takes_bambuddys_document_over_the_trees_parent(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    respx.get(f"{API}/local-presets/3").respond(
        200,
        json={
            "id": 3,
            "name": "My Printer",
            "preset_type": "printer",
            "source": "orcaslicer",
            "setting": {
                "type": "machine",
                "name": "My Printer",
                "inherits": "Test Printer 0.4 nozzle",
                "printable_height": "300",
            },
        },
    )
    local = plan.model_copy(update={"printer_preset": PresetRef(source="local", id="3")})
    files = await build_presets(local, profiles, client)
    machine = json.loads(files.machine)
    assert machine["printable_height"] == "300" and machine["nozzle_diameter"] == ["0.4"]
    assert files.from_bambuddy == {"printer": hashlib.sha256(files.machine).hexdigest()}


@respx.mock
async def test_a_local_preset_whose_parent_the_tree_lacks_is_refused(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    """Bambuddy's document is merged over OrcaSlicer's BBL tree, which may name a preset
    this Bambu Studio does not ship: refused, naming it, rather than sliced as a root
    the CLI fills from its defaults."""
    setting: dict[str, Any] = {**LOCAL_PLA["setting"], "inherits": "Generic PLA @BBL H2D"}
    doc = {**LOCAL_PLA, "setting": setting}
    respx.get(f"{API}/local-presets/7").respond(200, json=doc)
    local = plan.model_copy(update={"filament_presets": [PresetRef(source="local", id="7")]})
    with pytest.raises(
        ProfileError,
        match=r"local filament preset Inland.*over Bambu Studio 02\.08\.02\.61's presets:"
        r" .*inherits Generic PLA @BBL H2D: no such preset",
    ):
        await build_presets(local, profiles, client)


@respx.mock
async def test_a_local_preset_of_another_slot_is_refused(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    respx.get(f"{API}/local-presets/7").respond(200, json={**LOCAL_PLA, "preset_type": "process"})
    local = plan.model_copy(update={"filament_presets": [PresetRef(source="local", id="7")]})
    with pytest.raises(ProfileError, match=r"local preset 7 is a process preset"):
        await build_presets(local, profiles, client)


@respx.mock
async def test_a_local_preset_bambuddy_no_longer_has_is_refused(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    respx.get(f"{API}/local-presets/7").respond(404, json={"detail": "Local preset not found"})
    local = plan.model_copy(update={"filament_presets": [PresetRef(source="local", id="7")]})
    with pytest.raises(ProfileError, match=r"Bambuddy has no local preset 7 any more"):
        await build_presets(local, profiles, client)


@respx.mock
async def test_a_local_preset_without_settings_is_refused(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    # Bambuddy answers {} for a stored document it cannot parse.
    respx.get(f"{API}/local-presets/7").respond(200, json={**LOCAL_PLA, "setting": {}})
    local = plan.model_copy(update={"filament_presets": [PresetRef(source="local", id="7")]})
    with pytest.raises(ProfileError, match=r"holds no settings"):
        await build_presets(local, profiles, client)


async def test_a_local_id_that_is_no_number_is_refused(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    local = plan.model_copy(update={"filament_presets": [PresetRef(source="local", id="GFL")]})
    with pytest.raises(ProfileError, match=r"local filament preset GFL.*is a number"):
        await build_presets(local, profiles, client)


def test_a_local_preset_from_bambuddy_keys_apart(plan: SlicePlan) -> None:
    """An edit in Bambuddy rewrites a local preset under the same id, so the key carries
    each such file's digest, after the process's, in slot order."""
    image = native_preset_key(plan, 1, "02.08.02.61")
    filament = native_preset_key(plan, 1, "02.08.02.61", {"filament_2": "ab" * 32})
    edited = native_preset_key(plan, 1, "02.08.02.61", {"filament_2": "cd" * 32})
    assert len({image, filament, edited}) == 3
    assert filament.endswith(
        "/bambuddy-filament_2=" + "ab" * 32 + "/colours=#FF0000,#00FF00/flatten=2"
    )
    every = native_preset_key(
        plan, 1, "02.08.02.61", {"filament_1": "f1", "printer": "m", "process": "p"}
    )
    assert "/bambuddy-process=p/bambuddy-printer=m/bambuddy-filament_1=f1/colours=" in every


async def test_an_orca_cloud_preset_is_refused(profiles: Profiles, plan: SlicePlan) -> None:
    orca = plan.model_copy(update={"process_preset": PresetRef(source="orca_cloud", id="9")})
    with pytest.raises(ProfileError, match="orca_cloud process preset 9"):
        await build_presets(orca, profiles, None)


@respx.mock
async def test_a_cloud_ref_falls_back_to_its_name(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    respx.get(f"{API}/slicer/presets").respond(
        200,
        json={"cloud": {"filament": [{"source": "cloud", "id": "GF_OLD", "name": "Test PETG"}]}},
    )
    respx.get(f"{API}/local-presets/").respond(200, json={})
    renamed = plan.model_copy(update={"filament_presets": [PresetRef(source="cloud", id="GF_OLD")]})
    files = await build_presets(renamed, profiles, client)
    assert json.loads(files.filaments[0])["name"] == "Test PETG"


async def test_a_cloud_user_preset_without_a_file_is_refused(
    profiles: Profiles, plan: SlicePlan
) -> None:
    user = plan.model_copy(update={"filament_presets": [PresetRef(source="cloud", id="PFUS1")]})
    with pytest.raises(ProfileError, match="cloud filament preset PFUS1"):
        await build_presets(user, profiles, None)


@respx.mock
async def test_a_local_process_takes_bambuddys_values(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    values = respx.get(f"{API}/slicer/preset-values").respond(
        200,
        json={
            "resolved": True,
            "values": {"type": "process", "name": "Mine", "layer_height": "0.12"},
        },
    )
    respx.get(f"{API}/slicer/presets").respond(200, json={})
    respx.get(f"{API}/local-presets/").respond(200, json={})
    local = plan.model_copy(update={"process_preset": PresetRef(source="local", id="4")})
    process = json.loads((await build_presets(local, profiles, client)).process)
    assert process["layer_height"] == "0.12" and process["curr_bed_type"] == "Textured PEI Plate"
    assert values.calls.last.request.url.params["slot"] == "process"


@respx.mock
async def test_an_unresolved_local_process_carries_bambuddys_reason(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    respx.get(f"{API}/slicer/preset-values").respond(
        200, json={"resolved": False, "values": {}, "reason": "sidecar_unavailable"}
    )
    respx.get(f"{API}/slicer/presets").respond(200, json={})
    respx.get(f"{API}/local-presets/").respond(200, json={})
    local = plan.model_copy(update={"process_preset": PresetRef(source="local", id="4")})
    with pytest.raises(ProfileError, match="sidecar_unavailable"):
        await build_presets(local, profiles, client)


async def test_a_local_process_needs_bambuddy(profiles: Profiles, plan: SlicePlan) -> None:
    local = plan.model_copy(update={"process_preset": PresetRef(source="local", id="4")})
    with pytest.raises(ProfileError, match="local process preset 4"):
        await build_presets(local, profiles, None)


def test_native_preset_key_is_distinct_from_bambuddys(plan: SlicePlan) -> None:
    assert (
        native_preset_key(plan, 1, "02.08.02.61")
        == "native@02.08.02.61/"
        + slice_request(plan, 1).preset_key
        + "/colours=#FF0000,#00FF00/flatten=2"
    )


def test_a_recolour_is_another_native_slice(plan: SlicePlan) -> None:
    """#3962: the colours reach the CLI and change the sliced file, so two plans that
    differ only in them never share a slice; the case of a colour does not matter."""
    swapped = plan.model_copy(update={"filament_colours": ["#00FF00", "#FF0000"]})
    lower = plan.model_copy(update={"filament_colours": ["#ff0000", "#00ff00"]})
    key = native_preset_key(plan, 1, "02.08.02.61")
    assert native_preset_key(swapped, 1, "02.08.02.61") != key
    assert native_preset_key(lower, 1, "02.08.02.61") == key


@pytest.mark.requires_slicer_profiles
def test_the_images_h2c_machine_flattens() -> None:
    profiles = Profiles.installed(SLICER_PROFILES_DIR)
    assert profiles is not None and "printable_height" in profiles.machine(
        "Bambu Lab H2C 0.4 nozzle"
    )
    # Bambuddy's cloud ids are these setting_ids (Profiles' docstring).
    assert profiles.machine("GM041") == profiles.machine("Bambu Lab H2C 0.4 nozzle")
    # Its G-code comes only through `include` (#4714): the H2C's own start, which
    # loads the first filament, not fdm_machine_common's generic one.
    h2c = profiles.machine("Bambu Lab H2C 0.4 nozzle")
    start = h2c["machine_start_gcode"]
    assert isinstance(start, str) and start.startswith(";===== machine: H2C")
    assert "T[initial_no_support_filament_id]" in start
    assert "T[next_filament_id]" in h2c["change_filament_gcode"]
    assert profiles.version == "02.08.02.61"


def test_per_filament_lists_are_cut_to_the_filament_count(profiles: Profiles) -> None:
    # Preset::normalize resizes each per-filament list to filament_diameter's length;
    # Fiberon PA12-CF @BBL H2D's nozzle_temperature_range_low is ["…", "…"] over one
    # filament in v02.08.02.61. The AMS drying options are not resized.
    flat = profiles.filament("Test PA-CF")
    assert flat["nozzle_temperature_range_low"] == ["260"]
    assert flat["filament_dev_ams_drying_time"] == ["8", "12"]


def test_a_per_filament_list_too_short_is_refused(profiles: Profiles) -> None:
    with pytest.raises(ProfileError, match=r"Test Short.*nozzle_temperature_range_low"):
        profiles.filament("Test Short")


@respx.mock
async def test_a_cloud_user_process_takes_bambuddys_values(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    values = respx.get(f"{API}/slicer/preset-values").respond(
        200, json={"resolved": True, "values": {"type": "process", "layer_height": "0.12"}}
    )
    respx.get(f"{API}/slicer/presets").respond(200, json={})
    respx.get(f"{API}/local-presets/").respond(200, json={})
    user = plan.model_copy(update={"process_preset": PresetRef(source="cloud", id="PPUS1")})
    files = await build_presets(user, profiles, client)
    assert json.loads(files.process)["layer_height"] == "0.12"
    assert values.calls.last.request.url.params["source"] == "cloud"
    assert files.from_bambuddy == {"process": hashlib.sha256(files.process).hexdigest()}


@respx.mock
async def test_a_cloud_user_process_bambuddy_cannot_read_is_refused(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    respx.get(f"{API}/slicer/preset-values").respond(
        200, json={"resolved": False, "values": {}, "reason": "preset_unresolved"}
    )
    respx.get(f"{API}/slicer/presets").respond(200, json={})
    respx.get(f"{API}/local-presets/").respond(200, json={})
    user = plan.model_copy(update={"process_preset": PresetRef(source="cloud", id="PPUS1")})
    with pytest.raises(ProfileError, match=r"cloud process preset PPUS1: preset_unresolved"):
        await build_presets(user, profiles, client)


async def test_a_process_from_the_image_has_no_bambuddy_digest(
    profiles: Profiles, plan: SlicePlan
) -> None:
    assert (await build_presets(plan, profiles, None)).from_bambuddy == {}


def test_a_process_from_bambuddy_keys_apart(plan: SlicePlan) -> None:
    image = native_preset_key(plan, 1, "02.08.02.61")
    bambuddy = native_preset_key(plan, 1, "02.08.02.61", {"process": "ab" * 32})
    other = native_preset_key(plan, 1, "02.08.02.61", {"process": "cd" * 32})
    assert len({image, bambuddy, other}) == 3
    assert bambuddy.startswith("native@02.08.02.61/")
    # The segment a slice keyed before the other slots were keyed (#4233) still has.
    assert "/bambuddy-process=" + "ab" * 32 + "/colours=" in bambuddy


def test_a_list_over_a_scalar_option_is_refused() -> None:
    with pytest.raises(ProfileError, match=r"wall_loops.*list.*single value"):
        apply_overrides({"wall_loops": "2"}, {"wall_loops": ["3", "3"]})


def test_a_list_over_an_option_the_preset_leaves_out_is_written_whole() -> None:
    assert apply_overrides({}, {"outer_wall_speed": ["150", "150"]}) == {
        "outer_wall_speed": ["150", "150"]
    }


def _tree(tmp_path: Path) -> Path:
    shutil.copytree(PROFILES, tmp_path / "profiles")
    return tmp_path / "profiles"


def test_a_listed_file_missing_is_a_profile_error(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "BBL/process/mid.json").unlink()
    with pytest.raises(ProfileError, match=r"process/mid\.json"):
        Profiles(root).process("leaf")


def test_a_corrupt_file_is_a_profile_error(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "BBL/filament/Test PLA.json").write_text("{not json")
    with pytest.raises(ProfileError, match=r"filament/Test PLA\.json"):
        Profiles(root).filament("Test PLA")


def test_a_corrupt_vendor_file_is_a_profile_error(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "BBL.json").write_text("")
    with pytest.raises(ProfileError, match=r"BBL\.json"):
        Profiles(root).machine("GM_TEST")


@respx.mock
async def test_a_list_override_applies_over_bambuddys_vector_values(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    # The shape measured 2026-10-18: Bambuddy's preset-values answers a vector option as
    # a list (0.20mm Standard @BBL H2C's outer_wall_speed, one value per variant).
    respx.get(f"{API}/slicer/preset-values").respond(
        200,
        json={
            "resolved": True,
            "values": {
                "type": "process",
                "outer_wall_speed": ["200", "500", "500", "200", "500"],
                "wall_loops": "2",
            },
        },
    )
    respx.get(f"{API}/slicer/presets").respond(200, json={})
    respx.get(f"{API}/local-presets/").respond(200, json={})
    user = plan.model_copy(
        update={
            "process_preset": PresetRef(source="cloud", id="PPUS1"),
            "process_overrides": {
                "outer_wall_speed": ["150", "500", "500", "150", "500"],
                "wall_loops": "3",
            },
        }
    )
    process = json.loads((await build_presets(user, profiles, client)).process)
    assert process["outer_wall_speed"] == ["150", "500", "500", "150", "500"]
    assert process["wall_loops"] == "3"


@pytest.mark.parametrize(
    ("vendor", "where"),
    [
        ({"version": "1", "process_list": [{"sub_path": "process/leaf.json"}]}, "process_list"),
        ({"version": "1", "process_list": [{"name": "leaf"}]}, "process_list"),
        ({"version": "1", "process_list": "leaf"}, "process_list"),
    ],
)
def test_a_malformed_vendor_entry_is_a_profile_error(
    tmp_path: Path, vendor: dict[str, object], where: str
) -> None:
    root = _tree(tmp_path)
    (root / "BBL.json").write_text(json.dumps(vendor))
    with pytest.raises(ProfileError, match=rf"BBL\.json.*{where}"):
        Profiles(root).process("leaf")


def test_a_vendor_file_without_a_version_is_a_profile_error(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "BBL.json").write_text(json.dumps({"process_list": []}))
    with pytest.raises(ProfileError, match=r"BBL\.json.*version"):
        assert Profiles(root).profile_version


async def test_only_a_preset_outside_the_tree_needs_bambuddy(
    profiles: Profiles, plan: SlicePlan
) -> None:
    assert not await needs_bambuddy(plan, profiles)
    local = plan.model_copy(update={"process_preset": PresetRef(source="local", id="4")})
    assert await needs_bambuddy(local, profiles)
    unknown = plan.model_copy(update={"filament_presets": [PresetRef(source="cloud", id="GF?")]})
    assert await needs_bambuddy(unknown, profiles)
    # A local printer or filament preset is Bambuddy's document (#4233).
    printer = plan.model_copy(update={"printer_preset": PresetRef(source="local", id="3")})
    assert await needs_bambuddy(printer, profiles)
    # Refused with or without Bambuddy: opening one would change nothing.
    orca = plan.model_copy(update={"printer_preset": PresetRef(source="orca_cloud", id="3")})
    assert not await needs_bambuddy(orca, profiles)


async def test_a_name_lookup_with_no_bambuddy_says_so(profiles: Profiles, plan: SlicePlan) -> None:
    unknown = plan.model_copy(update={"filament_presets": [PresetRef(source="cloud", id="GF?")]})
    with pytest.raises(ProfileError, match="needs Bambuddy, and none is configured"):
        await build_presets(unknown, profiles, None)
