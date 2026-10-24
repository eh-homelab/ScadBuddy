"""#3962: every filament slot of a Bambuddy slice refs a preset of its own name, so the
slicer keeps the prime tower when slots share one preset."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.bambuddy.catalogue import Catalogue, PresetChoice, _build
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.models import LocalPresetCatalogue, PresetCatalogue, PresetRef
from scadbuddy.bambuddy.slot_presets import SlotPresets, is_slot_preset, slot_preset_name
from tests.bambuddy.conftest import BASE_URL

API = f"{BASE_URL}/api/v1"
PLA = "Bambu PLA Basic @BBL H2C"
STANDARD = PresetRef(source="standard", id=PLA)
PROCESS = PresetRef(source="standard", id="0.20mm Standard @BBL H2C")
PRINTER = PresetRef(source="standard", id="Bambu Lab H2C 0.4 nozzle")


def catalogue(*extra: PresetChoice) -> Catalogue:
    return Catalogue(
        printer=[],
        process=[],
        filament=[PresetChoice(ref=STANDARD, name=PLA), *extra],
    )


def plan(*filaments: PresetRef) -> SlicePlan:
    return SlicePlan(
        printer_preset=PRINTER,
        process_preset=PROCESS,
        filament_presets=list(filaments),
        filament_colours=["#FF0000", "#00FF00", "#0000FF"][: len(filaments)],
        bed_type="Textured PEI Plate",
    )


def stub(slot: int, base: str = PLA) -> dict[str, Any]:
    return {
        "name": slot_preset_name(base, slot),
        "inherits": base,
        "from": "system",
        "type": "filament",
    }


def row(preset_id: int, name: str, **extra: Any) -> dict[str, Any]:
    return {"id": preset_id, "name": name, "preset_type": "filament", "source": "manual", **extra}


def listed(*rows: dict[str, Any]) -> respx.Route:
    return respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(
            200, json={"filament": list(rows), "printer": [], "process": []}
        )
    )


def creates(first_id: int = 500) -> respx.Route:
    ids = iter(range(first_id, first_id + 100))

    def made(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        return httpx.Response(200, json=row(next(ids), sent["name"]))

    return respx.post(f"{API}/local-presets/").mock(side_effect=made)


def sent(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


@respx.mock
async def test_repeats_of_one_preset_ref_distinct_presets_inheriting_it(
    bambuddy: BambuddyClient,
) -> None:
    listed()
    create = creates()

    shared = plan(STANDARD, STANDARD, STANDARD)

    out = await SlotPresets(bambuddy, catalogue()).plan(shared)

    # The first slot keeps the shared preset; each repeat refs its own.
    assert out.filament_presets == [
        STANDARD,
        PresetRef(source="local", id="500"),
        PresetRef(source="local", id="501"),
    ]
    assert sent(create) == [
        {"name": f"{PLA} (ScadBuddy slot 2)", "preset_type": "filament", "setting": stub(2)},
        {"name": f"{PLA} (ScadBuddy slot 3)", "preset_type": "filament", "setting": stub(3)},
    ]
    # Everything but the filaments is the plan as it was.
    assert out.model_dump(exclude={"filament_presets"}) == shared.model_dump(
        exclude={"filament_presets"}
    )


@respx.mock
async def test_an_existing_preset_of_that_name_and_document_is_reused(
    bambuddy: BambuddyClient,
) -> None:
    listed(row(40, f"{PLA} (ScadBuddy slot 2)"), row(41, "Someone's own PLA"))
    respx.get(f"{API}/local-presets/40").mock(
        return_value=httpx.Response(200, json=row(40, f"{PLA} (ScadBuddy slot 2)", setting=stub(2)))
    )
    create = creates()
    presets = SlotPresets(bambuddy, catalogue())

    first = await presets.plan(plan(STANDARD, STANDARD))
    # A second plate of the same run asks nothing of Bambuddy again.
    second = await presets.plan(plan(STANDARD, STANDARD))

    assert first.filament_presets == [STANDARD, PresetRef(source="local", id="40")]
    assert second.filament_presets == first.filament_presets
    assert not create.called


@respx.mock
async def test_a_same_named_preset_holding_another_document_is_not_reused(
    bambuddy: BambuddyClient,
) -> None:
    """Someone edited it in Bambuddy: it is left alone, never deleted, and a new one made."""
    listed(row(40, f"{PLA} (ScadBuddy slot 2)"))
    respx.get(f"{API}/local-presets/40").mock(
        return_value=httpx.Response(
            200, json=row(40, f"{PLA} (ScadBuddy slot 2)", setting={**stub(2), "x": "1"})
        )
    )
    create = creates()

    out = await SlotPresets(bambuddy, catalogue()).plan(plan(STANDARD, STANDARD))

    assert out.filament_presets == [STANDARD, PresetRef(source="local", id="500")]
    assert create.call_count == 1


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(403, json={"detail": "API key does not have permission"}),
        httpx.Response(500, text="boom"),
        httpx.ConnectError("refused"),
    ],
)
@respx.mock
async def test_a_preset_that_cannot_be_made_leaves_the_shared_ref(
    bambuddy: BambuddyClient, answer: httpx.Response | Exception
) -> None:
    listed()
    route = respx.post(f"{API}/local-presets/")
    if isinstance(answer, Exception):
        route.mock(side_effect=answer)
    else:
        route.mock(return_value=answer)
    shared = plan(STANDARD, STANDARD)

    out = await SlotPresets(bambuddy, catalogue()).plan(shared)

    assert out == shared


@respx.mock
async def test_an_unreadable_list_leaves_the_shared_ref(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/local-presets/").mock(return_value=httpx.Response(502))
    create = creates()
    shared = plan(STANDARD, STANDARD)

    assert await SlotPresets(bambuddy, catalogue()).plan(shared) == shared
    assert not create.called


@respx.mock
async def test_a_local_presets_repeat_is_a_copy_of_its_document(bambuddy: BambuddyClient) -> None:
    own = {"name": "My PETG", "inherits": "Generic PETG @BBL H2C", "nozzle_temperature": ["250"]}
    respx.get(f"{API}/local-presets/7").mock(
        return_value=httpx.Response(200, json=row(7, "My PETG", source="orca", setting=own))
    )
    listed(row(7, "My PETG", source="orca"))
    create = creates()
    local = PresetRef(source="local", id="7")
    names = catalogue(PresetChoice(ref=local, name="My PETG"))

    out = await SlotPresets(bambuddy, names).plan(plan(local, local))

    assert out.filament_presets == [local, PresetRef(source="local", id="500")]
    assert sent(create)[0]["setting"] == {**own, "name": "My PETG (ScadBuddy slot 2)"}


@respx.mock
async def test_a_cloud_ref_named_as_a_bundled_preset_counts_as_a_repeat_of_it(
    bambuddy: BambuddyClient,
) -> None:
    """The CLI compares names, so a cloud ref of Bambu PLA Basic beside the standard
    one is the same preset to it."""
    listed()
    create = creates()
    cloud = PresetRef(source="cloud", id="GFSA00_14")
    names = catalogue(PresetChoice(ref=cloud, name=PLA))

    out = await SlotPresets(bambuddy, names).plan(plan(STANDARD, cloud))

    assert out.filament_presets == [STANDARD, PresetRef(source="local", id="500")]
    assert sent(create)[0]["setting"] == stub(2)


@respx.mock
async def test_a_cloud_user_preset_is_left_shared_and_nothing_is_written(
    bambuddy: BambuddyClient,
) -> None:
    """Its document cannot be read from here, and a stub of a name the sidecar does not
    bundle would slice from the CLI's defaults."""
    create = creates()
    cloud = PresetRef(source="cloud", id="PFUS123")
    names = catalogue(PresetChoice(ref=cloud, name="My cloud PLA"))
    shared = plan(cloud, cloud)

    assert await SlotPresets(bambuddy, names).plan(shared) == shared
    assert not create.called


@respx.mock
async def test_distinct_presets_ask_nothing_of_bambuddy(bambuddy: BambuddyClient) -> None:
    other = PresetRef(source="standard", id="Bambu PETG HF @BBL H2C")
    distinct = plan(STANDARD, other)

    assert await SlotPresets(bambuddy, catalogue()).plan(distinct) is distinct
    assert not respx.calls


def test_slot_presets_are_never_offered_in_the_catalogue() -> None:
    local = LocalPresetCatalogue.model_validate(
        {
            "filament": [
                row(1, "My PETG", source="orca"),
                row(2, f"{PLA} (ScadBuddy slot 2)"),
                row(3, "ScadBuddy slot 2 PLA"),
            ]
        }
    )

    built = _build(PresetCatalogue(), local)

    assert [choice.name for choice in built.filament] == ["My PETG", "ScadBuddy slot 2 PLA"]


def test_only_scadbuddys_own_names_are_slot_presets() -> None:
    assert is_slot_preset(slot_preset_name(PLA, 12))
    assert not is_slot_preset(PLA)
    assert not is_slot_preset(f"{PLA} (slot 2)")
    assert not is_slot_preset(f"{PLA} (ScadBuddy slot 2) copy")
