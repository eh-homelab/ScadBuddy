from __future__ import annotations

from typing import Any

import httpx
import psycopg
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import StoredSettings
from tests.api.test_print import printers_route
from tests.api.test_print_filaments import inventory_routes, prepared
from tests.api.test_send import BASE, upload_route
from tests.bambuddy.conftest import recording

API = f"{BASE}/api/v1"


def h2c_presets() -> None:
    respx.get(f"{API}/slicer/presets").mock(
        return_value=httpx.Response(200, json=recording("slicer-presets-h2c.json"))
    )
    respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=recording("local-presets.json"))
    )


@respx.mock
def test_choices_offer_every_size_the_rack_and_the_last_plate(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=recording("printer-status-rack.json"))
    )
    respx.get(f"{API}/archives/").mock(
        return_value=httpx.Response(200, json=recording("archives.json"))
    )

    body = client.get(f"/api/v1/print/outputs/{output_id}/choices?printer_id=1").json()

    assert body["nozzle_sizes"] == ["0.2", "0.4", "0.6", "0.8"]
    assert {(n["size"], n["flow"]) for n in body["installed"]} >= {("0.2", "standard")}
    assert body["tiers"]["0.2"][0] == {
        "tier": "fine",
        "process_name": "0.08mm High Quality @BBL H2C 0.2 nozzle",
    }
    assert "0.08mm High Quality @BBL H2C" in body["processes"]["0.4"]
    assert body["last_bed_type"] == "Textured PEI Plate"
    assert body["filaments"]["slots"]


MIXED_ARCHIVES = [
    {"id": 1, "printer_id": 1, "status": "completed", "bed_type": "Cool Plate"},
    {
        "id": 2,
        "printer_id": 1,
        "status": "completed",
        "bed_type": "Engineering Plate",
        "started_at": "2026-09-27T04:09:36Z",
    },
    {
        "id": 3,
        "printer_id": 1,
        "status": "completed",
        "bed_type": "Supertack Plate",
        "started_at": "2026-09-27T09:00:00",
    },
]
"""Null, aware and naive timestamps together (PR #335 review 3). Newest is row 3, its
naive time read as UTC."""


@respx.mock
def test_archives_with_null_and_mixed_timestamps_still_open_the_dialog(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    respx.get(f"{API}/archives/").mock(return_value=httpx.Response(200, json=MIXED_ARCHIVES))

    response = client.get(f"/api/v1/print/outputs/{output_id}/choices?printer_id=1")

    assert response.status_code == 200, response.text
    assert response.json()["last_bed_type"] == "Supertack Plate"
    assert response.json()["bed_type"] == "Supertack Plate"


@respx.mock
def test_an_offline_printer_still_opens_the_dialog(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    respx.get(f"{API}/archives/").mock(return_value=httpx.Response(200, json=[]))

    response = client.get(f"/api/v1/print/outputs/{output_id}/choices?printer_id=1")

    assert response.status_code == 200
    assert response.json()["installed"] == []
    assert response.json()["bed_type"] == "Textured PEI Plate"


@respx.mock
def test_choices_list_the_filament_presets_each_nozzle_size_takes(
    client: TestClient, model: str
) -> None:
    """Task 9 R5 — Advanced mode's per-slot override picks from these."""
    output_id = prepared(client, model)
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    respx.get(f"{API}/archives/").mock(return_value=httpx.Response(200, json=[]))

    body = client.get(f"/api/v1/print/outputs/{output_id}/choices?printer_id=1").json()

    by_size = body["filament_presets"]
    assert set(by_size) == {"0.2", "0.4", "0.6", "0.8"}
    names_02 = [row["name"] for row in by_size["0.2"]]
    assert "Bambu ABS @BBL H2C 0.2 nozzle" in names_02
    assert "Bambu ABS @BBL H2C" not in names_02
    # A user's own (OrcaSlicer-imported) profile is offered too.
    assert "Cookiecad PETG Magic Dark Magic (3DFP 7JdoWkaDB) @H2C 0.2n" in names_02
    # One row per name: the cloud and standard tiers list the same preset twice, and the
    # cloud ref is the one the resolver itself prefers.
    assert len(names_02) == len(set(names_02))
    abs_02 = next(row for row in by_size["0.2"] if row["name"] == "Bambu ABS @BBL H2C 0.2 nozzle")
    assert abs_02["ref"] == {"source": "cloud", "id": "GFSB00_23"}
    # Fix round 1 #7 — the dialog reads only the name and the ref.
    assert {key for row in by_size["0.4"] for key in row} == {"ref", "name"}


@respx.mock
def test_choices_carry_what_this_model_last_printed_with(client: TestClient, model: str) -> None:
    """The dialog reopens on the remembered spools; only the run writes them."""
    client.put(
        f"/api/v1/print/models/{model}/choices",
        json={"printer_id": 1, "filament_plan": [{"slot_id": 1, "spool_id": 22}]},
    )
    output_id = prepared(client, model)
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    respx.get(f"{API}/archives/").mock(return_value=httpx.Response(200, json=[]))

    body = client.get(f"/api/v1/print/outputs/{output_id}/choices").json()

    assert body["model_choices"] == {
        "printer_id": 1,
        "filament_plan": [{"slot_id": 1, "spool_id": 22}],
        "nozzles": [],
        "tier": None,
        "process_name": None,
    }


@respx.mock
def test_the_dialogs_nozzles_tier_and_process_are_remembered_per_model(
    client: TestClient, model: str
) -> None:
    """Spec §7 — the dialog reopens on the last choices for this model."""
    remembered = {
        "printer_id": 1,
        "filament_plan": [],
        "nozzles": [{"size": "0.2", "flow": "standard"}, {"size": "0.2", "flow": "high_flow"}],
        "tier": None,
        "process_name": "0.06mm Fine @BBL H2C 0.2 nozzle",
    }
    assert client.put(f"/api/v1/print/models/{model}/choices", json=remembered).json() == (
        remembered
    )
    output_id = prepared(client, model)
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    respx.get(f"{API}/archives/").mock(return_value=httpx.Response(200, json=[]))

    body = client.get(f"/api/v1/print/outputs/{output_id}/choices").json()

    assert body["model_choices"] == remembered


def _store_choices_row(settings: Settings, model: str, choices: dict[str, Any]) -> None:
    """Write a ``model_print_choices`` row by hand, as an older writer might have."""
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            "INSERT INTO model_print_choices (model_id, choices) VALUES (%s, %s)",
            (model, Jsonb(choices)),
        )


def _load(app: FastAPI) -> StoredSettings:
    loaded: StoredSettings = getattr(app.state, STATE_ATTR).settings_store.load()
    return loaded


def test_a_stored_choice_with_only_the_printer_and_spools_still_loads(
    client: TestClient, app: FastAPI, model: str, settings: Settings
) -> None:
    """Every dialog choice defaults to "nothing remembered", so a row without them loads."""
    _store_choices_row(
        settings, model, {"printer_id": 2, "filament_plan": [{"slot_id": 1, "spool_id": 9}]}
    )

    loaded = _load(app)

    choices = loaded.model_print_choices[model]
    assert choices.printer_id == 2
    assert choices.nozzles == []
    assert choices.tier is None
    assert choices.process_name is None


# --- final review 2: a remembered printer that is no longer active ---------------------


def other_printers_route() -> respx.Route:
    """Printer 1 (the recording) plus an inactive printer 7."""
    rows = [
        *recording("printers.json"),
        {"id": 7, "name": "Retired", "model": "H2C", "is_active": False},
    ]
    return respx.get(f"{API}/printers/").mock(return_value=httpx.Response(200, json=rows))


def printer_1_hardware() -> None:
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    respx.get(f"{API}/archives/").mock(return_value=httpx.Response(200, json=[]))


@respx.mock
def test_a_remembered_printer_no_longer_active_falls_through_to_an_active_one(
    client: TestClient, model: str
) -> None:
    client.put(f"/api/v1/print/models/{model}/choices", json={"printer_id": 7})
    output_id = prepared(client, model)
    upload_route()
    other_printers_route()
    inventory_routes()
    h2c_presets()
    printer_1_hardware()

    body = client.get(f"/api/v1/print/outputs/{output_id}/choices").json()

    assert body["printer_id"] == 1
    assert [row["id"] for row in body["printers"]] == [1]


@respx.mock
def test_a_settings_printer_that_is_gone_falls_through_to_the_first_active_one(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    client.put(
        "/api/v1/settings",
        json={"bambuddy_url": BASE, "bambuddy_api_key": "s3cret", "printer_id": 42},
    )
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    printer_1_hardware()

    body = client.get(f"/api/v1/print/outputs/{output_id}/choices").json()

    assert body["printer_id"] == 1


def test_a_single_remembered_nozzle_is_read_as_both_sides(
    client: TestClient, app: FastAPI, model: str, settings: Settings
) -> None:
    """Final review 10: the dialog has two sides, so a remembered choice is none or two.
    One entry (hand-edited, or an older writer) means that nozzle on both sides."""
    one = {"nozzles": [{"size": "0.6", "flow": "standard"}]}

    saved = client.put(f"/api/v1/print/models/{model}/choices", json=one).json()

    both = [{"size": "0.6", "flow": "standard"}, {"size": "0.6", "flow": "standard"}]
    assert saved["nozzles"] == both
    _store_choices_row(settings, "another-model", one)
    loaded = _load(app)
    assert [n.model_dump() for n in loaded.model_print_choices["another-model"].nozzles] == both


def test_more_than_two_remembered_nozzles_are_refused(client: TestClient, model: str) -> None:
    three = {"nozzles": [{"size": "0.4"}] * 3}
    assert client.put(f"/api/v1/print/models/{model}/choices", json=three).status_code == 422
