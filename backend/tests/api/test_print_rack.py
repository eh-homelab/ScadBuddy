"""The rack nozzle routes (#836): the remembered algorithm, the preview, the manual pick."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Sequence
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.components import getter_for
from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.rack.component import RACK_USAGE
from scadbuddy.rack.rank import Usage
from scadbuddy.rack.usage import PickedHotend, RackUsageStore
from tests.api.test_print_filaments import prepared, queue_route, slice_routes
from tests.api.test_print_library import library_file, one_color, run_library
from tests.api.test_print_run_choices import (
    API,
    body,
    grouped_requirements_route,
    run_print,
    run_routes,
)
from tests.api.test_send import configure, upload_route
from tests.bambuddy.conftest import recording
from tests.rack.helpers import INVENTED_SERIALS, invented_status, serial

pytestmark = pytest.mark.requires_postgres


def test_the_rack_algorithm_is_remembered_per_printer_and_forgotten(client: TestClient) -> None:
    put = client.put("/api/v1/print/printers/1/rack-algorithm", json={"algorithm": "newest_first"})
    assert put.status_code == 200, put.text
    assert put.json() == {"printer_id": 1, "algorithm": "newest_first"}
    remembered = client.get("/api/v1/settings/remembered").json()
    assert remembered["printer_rack_algorithms"] == {"1": "newest_first"}

    forgot = client.put("/api/v1/print/printers/1/rack-algorithm", json={"algorithm": None})
    assert forgot.json() == {"printer_id": 1, "algorithm": "least_used"}
    assert client.get("/api/v1/settings/remembered").json().get("printer_rack_algorithms", {}) == {}


def test_an_unknown_algorithm_is_refused(client: TestClient) -> None:
    response = client.put("/api/v1/print/printers/1/rack-algorithm", json={"algorithm": "random"})
    assert response.status_code == 422


def rack_usage(client: TestClient) -> RackUsageStore:
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    store: RackUsageStore = state.components.get(RACK_USAGE)
    return store


def invented_rack_route() -> None:
    """The recorded rack with invented serials; replaces ``hardware_routes``' answer
    (respx re-uses a route registered again with the same pattern)."""
    respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=invented_status())
    )


@respx.mock
def test_the_check_records_the_racks_hotends_as_seen(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    invented_rack_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/check", json=body())

    assert response.status_code == 200, response.text
    usage = asyncio.run(rack_usage(client).usage([serial(17), serial(0), serial(1)]))
    assert usage[serial(17)].first_seen_at is not None
    assert usage[serial(0)].first_seen_at is not None
    assert usage[serial(1)].first_seen_at is None  # the left hotend is not a rack hotend


class BrokenUsage(RackUsageStore):
    """Reads work as an empty history; the picks write fails."""

    def __init__(self) -> None:
        pass

    async def seen(self, printer_id: int, serials: Iterable[str]) -> None:
        return None

    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]:
        return {}

    async def record_picks(
        self, queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]
    ) -> int:
        raise RuntimeError("the database went away")

    def close(self) -> None:
        return None


@respx.mock
def test_a_failed_picks_write_still_returns_the_queued_run(client: TestClient, model: str) -> None:
    """Spec §5: the item is queued, so the write is advisory."""
    client.app.dependency_overrides[getter_for(RACK_USAGE)] = BrokenUsage  # type: ignore[attr-defined]
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    grouped_requirements_route()
    slice_routes()
    queued = queue_route()

    response = run_print(client, output_id, json=body(nozzles=[{"size": "0.4"}], tier="standard"))

    assert response.status_code == 200, response.text
    assert response.json()["queue_item_ids"] == [51]
    assert json.loads(queued.calls.last.request.content)["nozzle_rack_choice"] == {"0": 4}


@respx.mock
def test_the_picks_are_recorded_against_the_queue_item(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    invented_rack_route()
    grouped_requirements_route()
    slice_routes()
    queue_route()

    response = run_print(client, output_id, json=body(nozzles=[{"size": "0.4"}], tier="standard"))

    assert response.status_code == 200, response.text
    assert asyncio.run(rack_usage(client).picked_items([51])) == {51}
    # Spec §5/§7: the result reports the picks sent, by position, never by serial.
    assert response.json()["rack_picks"] == [
        {
            "plate_id": 1,
            "group_id": 0,
            "position": 4,
            "reason": None,
            "unsafe_material": False,
            "glow_unchecked": False,
            "options": [],
        }
    ]
    assert serial(19) not in response.text


@respx.mock
def test_a_library_run_sends_its_pick_but_records_none(client: TestClient) -> None:
    """The watcher never settles a library print (its record and settle hook are keyed
    by output id), so a pick row would never be credited."""
    configure(client)
    one_color(89)
    library_file(89)
    run_routes()
    invented_rack_route()
    grouped_requirements_route()
    slice_routes()
    queued = queue_route()

    response = run_library(
        client,
        89,
        json={
            **body(nozzles=[{"size": "0.4"}], tier="standard"),
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
        },
    )

    assert response.status_code == 200, response.text
    assert json.loads(queued.calls.last.request.content)["nozzle_rack_choice"] == {"0": 4}
    assert asyncio.run(rack_usage(client).picked_items([51])) == set()


def test_the_rack_feature_hooks_the_settle_write_into_the_watcher(client: TestClient) -> None:
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    assert len(state.print_watcher.on_settled) == 1


CHECK_04 = {"nozzles": [{"size": "0.4"}], "tier": "standard"}


def check(client: TestClient, output_id: str, **extra: Any) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/print/outputs/{output_id}/check", json={**body(**CHECK_04), **extra}
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


@respx.mock
def test_the_check_previews_the_rack_sides_eligible_positions(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()

    rack = check(client, output_id)["rack"]

    assert [option["position"] for option in rack["options"]] == [2, 4, 6]
    assert {option["flow"] for option in rack["options"]} == {"standard"}
    assert rack["position"] in (2, 4, 6) and rack["reason"]
    assert (rack["group_id"], rack["plate_id"]) == (None, None)


@respx.mock
def test_a_manual_pick_that_fits_is_previewed_and_sent(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    grouped_requirements_route(color="#00629B")  # position 2's color: the ranking prefers 2
    slice_routes()
    queued = queue_route()

    rack = check(client, output_id, rack_position=6)["rack"]
    assert (rack["position"], rack["reason"]) == (6, "chosen by hand")

    response = run_print(client, output_id, json={**body(**CHECK_04), "rack_position": 6})
    assert response.status_code == 200, response.text
    assert json.loads(queued.calls.last.request.content)["nozzle_rack_choice"] == {"0": 6}


@respx.mock
def test_a_manual_pick_that_does_not_fit_is_refused_before_anything_is_sliced(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()

    message = (
        "Rack position 1 holds a 0.2 mm Standard nozzle, and this prints with a 0.4 mm "
        "Standard nozzle. Choose another position, or Automatic."
    )
    assert check(client, output_id, rack_position=1)["errors"] == [message]
    response = run_print(client, output_id, json={**body(**CHECK_04), "rack_position": 1})
    assert (response.status_code, response.json()["detail"]) == (422, message)
    assert not sliced.called


@respx.mock
def test_a_high_flow_choice_is_judged_as_the_standard_slice_it_becomes(
    client: TestClient, model: str
) -> None:
    """Review Focus 2 (#484): an HH position is neither offered nor accepted."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    high_flow = {"nozzles": [{"size": "0.4", "flow": "high_flow"}], "tier": "standard"}

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/check", json={**body(**high_flow), "rack_position": 3}
    )

    assert response.status_code == 200, response.text
    assert response.json()["errors"] == [
        "Rack position 3 holds a 0.4 mm High Flow nozzle, and this prints with a 0.4 mm "
        "Standard nozzle. Choose another position, or Automatic."
    ]
    assert [o["position"] for o in response.json()["rack"]["options"]] == [2, 4, 6]


@respx.mock
def test_an_abrasive_spool_is_warned_about_in_the_preview(client: TestClient, model: str) -> None:
    """The material table ships empty, so a CF spool always warns (spec §8, #1011)."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    spools = recording("inventory-spools.json")
    for spool in spools:
        spool["material"], spool["subtype"] = "PLA", "CF"
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=spools))

    result = check(client, output_id)

    assert result["rack"]["unsafe_material"] is True
    assert "rack-unsafe-material" in {w["kind"] for w in result["warnings"]}


@respx.mock
def test_an_unreadable_rack_previews_nothing_and_refuses_nothing(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))

    result = check(client, output_id, rack_position=3)

    assert (result["rack"], result["errors"]) == (None, [])


@respx.mock
def test_a_clear_spool_is_no_color_in_the_preview(client: TestClient, model: str) -> None:
    """Zero alpha is no color, never black: a Clear spool (``00000000``) does not prefer
    a hotend loaded with black."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    status = recording("printer-status-rack.json")
    for entry in status["nozzle_rack"]:
        if entry["id"] == 21:  # position 6
            entry["filament_color"] = "000000FF"
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status))
    clear = {"slots": [{"slot_id": 1, "spool_id": 15}]}

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/check",
        json={**body(**CHECK_04), "filament_plan": clear},
    )

    assert response.status_code == 200, response.text
    rack = response.json()["rack"]
    assert rack["position"] == 2
    assert rack["reason"] != "already loaded with this color"


@respx.mock
def test_no_serial_appears_anywhere_in_the_check_body(client: TestClient, model: str) -> None:
    """Spec §5, §9: searched as a string over the whole JSON, options included."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    invented_rack_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/check", json=body(**CHECK_04))

    assert response.status_code == 200, response.text
    assert response.json()["rack"]["options"], "the check must have ranked a rack to be a real test"
    assert not [s for s in INVENTED_SERIALS if s in response.text]
