"""Presets as files for the native slicer (#3822, plan 2026-10-16 Task 8)."""

import hashlib
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

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


def test_flatten_refuses_includes(profiles: Profiles) -> None:
    with pytest.raises(ProfileError, match=r"with_includes.*includes"):
        profiles.process("with_includes")


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


async def test_a_local_filament_is_refused_by_name(profiles: Profiles, plan: SlicePlan) -> None:
    local = plan.model_copy(update={"filament_presets": [PresetRef(source="local", id="7")]})
    with pytest.raises(ProfileError, match="local filament preset"):
        await build_presets(local, profiles, None)


async def test_a_local_printer_is_refused_by_name(profiles: Profiles, plan: SlicePlan) -> None:
    local = plan.model_copy(update={"printer_preset": PresetRef(source="local", id="3")})
    with pytest.raises(ProfileError, match=r"local printer preset 3.*only process"):
        await build_presets(local, profiles, None)


async def test_an_orca_cloud_preset_is_refused(profiles: Profiles, plan: SlicePlan) -> None:
    orca = plan.model_copy(update={"process_preset": PresetRef(source="orca_cloud", id="9")})
    with pytest.raises(ProfileError, match="orca_cloud process preset 9"):
        await build_presets(orca, profiles, None)


@respx.mock
async def test_a_local_filament_is_refused_by_its_catalogue_name(
    profiles: Profiles, plan: SlicePlan, client: BambuddyClient
) -> None:
    respx.get(f"{API}/slicer/presets").respond(200, json={})
    respx.get(f"{API}/local-presets/").respond(
        200,
        json={
            "filament": [
                {"id": 7, "name": "My PLA", "preset_type": "filament", "source": "orcaslicer"}
            ]
        },
    )
    local = plan.model_copy(update={"filament_presets": [PresetRef(source="local", id="7")]})
    with pytest.raises(ProfileError, match="local filament preset My PLA"):
        await build_presets(local, profiles, client)


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
        == "native@02.08.02.61/" + slice_request(plan, 1).preset_key
    )


@pytest.mark.requires_slicer_profiles
def test_the_images_h2c_machine_flattens() -> None:
    profiles = Profiles.installed(SLICER_PROFILES_DIR)
    assert profiles is not None and "printable_height" in profiles.machine(
        "Bambu Lab H2C 0.4 nozzle"
    )
    # Bambuddy's cloud ids are these setting_ids (Profiles' docstring).
    assert profiles.machine("GM041") == profiles.machine("Bambu Lab H2C 0.4 nozzle")
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
    assert files.bambuddy_process == hashlib.sha256(files.process).hexdigest()


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
    assert (await build_presets(plan, profiles, None)).bambuddy_process is None


def test_a_process_from_bambuddy_keys_apart(plan: SlicePlan) -> None:
    image = native_preset_key(plan, 1, "02.08.02.61")
    bambuddy = native_preset_key(plan, 1, "02.08.02.61", bambuddy_process="ab" * 32)
    other = native_preset_key(plan, 1, "02.08.02.61", bambuddy_process="cd" * 32)
    assert len({image, bambuddy, other}) == 3
    assert bambuddy.startswith("native@02.08.02.61/")


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
    # Refused with or without Bambuddy: opening one would change nothing.
    printer = plan.model_copy(update={"printer_preset": PresetRef(source="local", id="3")})
    assert not await needs_bambuddy(printer, profiles)


async def test_a_name_lookup_with_no_bambuddy_says_so(profiles: Profiles, plan: SlicePlan) -> None:
    unknown = plan.model_copy(update={"filament_presets": [PresetRef(source="cloud", id="GF?")]})
    with pytest.raises(ProfileError, match="needs Bambuddy, and none is configured"):
        await build_presets(unknown, profiles, None)
