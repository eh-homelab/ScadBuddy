"""Process preset values from the image's BBL profiles (#3818 tier A Task 3): what the
dialog's override checks read on a native run, with no Bambuddy."""

from __future__ import annotations

import pytest

from scadbuddy.bambuddy.filament_profiles import profile_overrides
from scadbuddy.bambuddy.models import PresetRef, PresetValues
from scadbuddy.bambuddy.prime_tower import PrimeTowerChoice, prime_tower_warnings
from scadbuddy.bambuddy.supports import support_overrides
from scadbuddy.slicing.profiles import Profiles
from scadbuddy.slicing.values import NOT_IN_PROFILES, native_values, values_for

LEAF = PresetRef(source="standard", id="leaf")


async def test_a_standard_ref_reads_the_flattened_process(profiles: Profiles) -> None:
    values = await native_values(profiles)(LEAF)
    assert values.resolved and values.reason is None
    assert values.values == profiles.process("leaf")
    assert values.values["wall_loops"] == "3" and values.values["layer_height"] == "0.16"


async def test_a_cloud_ref_reads_the_process_its_setting_id_names(profiles: Profiles) -> None:
    values = await native_values(profiles)(PresetRef(source="cloud", id="GP_LEAF"))
    assert values.resolved and values.values == profiles.process("leaf")


@pytest.mark.parametrize(
    "ref",
    [
        PresetRef(source="local", id="7"),
        PresetRef(source="orca_cloud", id="leaf"),
        PresetRef(source="cloud", id="PPUS1234"),
        # A base preset: never offered, so never read.
        PresetRef(source="standard", id="mid"),
        # A filament's setting_id: not a process.
        PresetRef(source="cloud", id="GF_PLA"),
    ],
)
async def test_a_ref_outside_the_trees_processes_is_unresolved(
    profiles: Profiles, ref: PresetRef
) -> None:
    values = await native_values(profiles)(ref)
    assert values == PresetValues(resolved=False, reason=NOT_IN_PROFILES)


async def test_a_preset_is_flattened_once_per_reader(
    profiles: Profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = profiles.process

    def counting(name: str) -> dict[str, str | list[str]]:
        calls.append(name)
        return real(name)

    monkeypatch.setattr(profiles, "process", counting)
    read = native_values(profiles)
    first, second = await read(LEAF), await read(PresetRef(source="cloud", id="GP_LEAF"))
    assert first == second and calls == ["leaf"]


async def test_profile_overrides_match_bambuddys_for_the_same_values(profiles: Profiles) -> None:
    flat = profiles.process("leaf")

    async def bambuddy(ref: PresetRef) -> PresetValues:
        return PresetValues(resolved=True, values=flat)

    native = await profile_overrides(None, LEAF, ["silk-pla"], read=native_values(profiles))
    assert native == await profile_overrides(None, LEAF, ["silk-pla"], read=bambuddy)
    assert native == ({"outer_wall_speed": ["50", "50"]}, [])


async def test_a_local_process_skips_a_profile_with_a_warning(profiles: Profiles) -> None:
    merged, warnings = await profile_overrides(
        None, PresetRef(source="local", id="7"), ["silk-pla"], read=native_values(profiles)
    )
    assert merged == {}
    assert len(warnings) == 1 and "not one of Bambu Studio's own" in warnings[0].message


async def test_supports_and_the_prime_tower_read_the_native_values(profiles: Profiles) -> None:
    read = native_values(profiles)
    supports, said = await support_overrides(None, LEAF, {"outer_wall_speed": "80"}, read=read)
    assert supports == {"outer_wall_speed": ["80", "80"]} and said == []
    # The fixture's process has no enable_prime_tower: a read preset says so.
    (tower,) = await prime_tower_warnings(None, LEAF, PrimeTowerChoice(enabled=True), read=read)
    assert tower.kind == "prime-tower-unconfirmed"


async def test_a_local_process_withholds_shaped_supports_naming_why(profiles: Profiles) -> None:
    read = native_values(profiles)
    _, said = await support_overrides(
        None, PresetRef(source="local", id="7"), {"outer_wall_speed": "80"}, read=read
    )
    assert said and "not one of Bambu Studio's own" in said[0].message
    assert "Bambuddy" not in said[0].message


async def test_values_for_native_needs_no_client(profiles: Profiles) -> None:
    assert (await values_for(None, native=profiles)(LEAF)).resolved


def test_values_for_without_either_is_refused() -> None:
    with pytest.raises(ValueError, match="Bambuddy client"):
        values_for(None, native=None)
