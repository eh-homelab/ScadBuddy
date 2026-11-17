"""`/api/v1/filament` (#6261): the filament catalogs, spool types and spools."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api import filament as filament_api
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.filament import actor
from scadbuddy.auth.models import ANONYMOUS, AuthSession, Person, PersonalToken, Service
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.printers.reports import PrinterReportStore
from tests.api.test_send import API, configure
from tests.support.printers import bambuddy_printer
from tests.support.sb_rows import seed_app_sb_spools

pytestmark = pytest.mark.requires_postgres

URL = "/api/v1/filament/catalog"


@pytest.fixture(autouse=True)
def _no_assignments_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test reads Bambuddy's assignments afresh (the route caches them, I-1)."""
    monkeypatch.setattr(filament_api, "_assignments_seen", None)


MISSING = "00000000-0000-4000-8000-000000000001"


def test_a_catalog_is_listed_made_changed_and_archived(client: TestClient) -> None:
    names = [row["name"] for row in client.get(f"{URL}/materials").json()]
    assert "PLA" in names
    made = client.post(f"{URL}/brands", json={"name": "Acme"})
    assert made.status_code == 201, made.text
    brand = made.json()
    assert client.get(f"{URL}/brands").json() == [brand]
    patched = client.patch(f"{URL}/brands/{brand['id']}", json={"name": "Acme 2"})
    assert patched.json()["name"] == "Acme 2"
    archived = client.patch(f"{URL}/brands/{brand['id']}", json={"archived": True})
    assert archived.json()["archived_at"] is not None
    assert client.get(f"{URL}/brands").json() == []
    assert len(client.get(f"{URL}/brands", params={"include_archived": True}).json()) == 1


def test_a_second_material_with_the_same_name_is_refused(client: TestClient) -> None:
    taken = client.post(f"{URL}/materials", json={"name": "pla"})
    assert taken.status_code == 409, taken.text


def test_an_unknown_kind_or_id_is_404(client: TestClient) -> None:
    assert client.get(f"{URL}/nope").status_code == 404
    assert client.post(f"{URL}/nope", json={"name": "x"}).status_code == 404
    assert client.patch(f"{URL}/brands/{MISSING}", json={"name": "x"}).status_code == 404


def test_a_bad_body_is_422(client: TestClient) -> None:
    assert client.post(f"{URL}/brands", json={"name": ""}).status_code == 422
    assert client.post(f"{URL}/brands", json={"name": "x", "bogus": 1}).status_code == 422
    bad_ref = client.post(f"{URL}/subtypes", json={"name": "S", "material_id": MISSING})
    assert bad_ref.status_code == 422, bad_ref.text


def test_a_backwards_temperature_range_on_a_material_or_subtype_is_422(
    client: TestClient,
) -> None:
    for kind in ("materials", "subtypes"):
        body = {"name": f"Hot {kind}", "nozzle_temp_min": 250, "nozzle_temp_max": 200}
        backwards = client.post(f"{URL}/{kind}", json=body)
        assert backwards.status_code == 422, backwards.text
        assert "nozzle_temp_min is above nozzle_temp_max" in backwards.text
        made = client.post(f"{URL}/{kind}", json={"name": f"Bed {kind}", "bed_temp_min": 50})
        assert made.status_code == 201, made.text
        path = f"{URL}/{kind}/{made.json()['id']}"
        assert client.patch(path, json={"bed_temp_max": 40}).status_code == 422
        assert client.patch(path, json={"bed_temp_max": 60}).json()["bed_temp_max"] == 60


TYPES = "/api/v1/filament/types"


def _material_id(client: TestClient) -> str:
    rows = client.get(f"{URL}/materials").json()
    found: str = next(row["id"] for row in rows if row["name"] == "PLA")
    return found


def _type(client: TestClient, **over: object) -> dict[str, object]:
    body = {
        "code": "PLA-RED",
        "material_id": _material_id(client),
        "rgba": "#ff0000",
        "label_weight_g": 1000,
    } | over
    made = client.post(TYPES, json=body)
    assert made.status_code == 201, made.text
    row: dict[str, object] = made.json()
    return row


def test_a_type_is_made_read_changed_and_archived(client: TestClient) -> None:
    made = _type(client, cost_per_kg="20")
    assert made["rgba"] == "#FF0000FF"
    assert made["filament_id"] is not None
    assert client.get(TYPES).json() == [made]
    one = client.get(f"{TYPES}/{made['id']}").json()
    assert one["presets"] == []
    assert one["k_values"] == []
    patched = client.patch(f"{TYPES}/{made['id']}", json={"colour_name": "Red"})
    assert patched.json()["colour_name"] == "Red"
    archived = client.patch(f"{TYPES}/{made['id']}", json={"archived": True})
    assert archived.json()["archived_at"] is not None
    assert client.get(TYPES).json() == []
    assert len(client.get(TYPES, params={"include_archived": True}).json()) == 1


def test_a_type_code_clash_is_409_and_a_backwards_range_is_422(client: TestClient) -> None:
    _type(client)
    base = {"material_id": _material_id(client), "rgba": "#00ff00", "label_weight_g": 1000}
    taken = client.post(TYPES, json={"code": "pla-red", **base})
    assert taken.status_code == 409, taken.text
    backwards = client.post(
        TYPES, json={"code": "X", "nozzle_temp_min": 300, "nozzle_temp_max": 200, **base}
    )
    assert backwards.status_code == 422, backwards.text


def test_a_missing_type_is_404(client: TestClient) -> None:
    assert client.get(f"{TYPES}/{MISSING}").status_code == 404
    assert client.patch(f"{TYPES}/{MISSING}", json={"note": "x"}).status_code == 404
    assert client.put(f"{TYPES}/{MISSING}/presets", json=[]).status_code == 404
    assert client.put(f"{TYPES}/{MISSING}/k-values", json=[]).status_code == 404


def test_presets_and_k_values_are_replaced(client: TestClient) -> None:
    made = _type(client)
    rows = [{"printer_model": "H2C", "nozzle_diameter": "0.4", "slicer_filament": "GFA00"}]
    saved = client.put(f"{TYPES}/{made['id']}/presets", json=rows)
    assert saved.status_code == 200, saved.text
    assert saved.json()[0]["slicer_filament"] == "GFA00"
    assert client.get(f"{TYPES}/{made['id']}").json()["presets"] == saved.json()
    twice = client.put(f"{TYPES}/{made['id']}/presets", json=[rows[0], rows[0]])
    assert twice.status_code == 422, twice.text
    k = {"printer_id": MISSING, "extruder": 0, "nozzle_diameter": "0.4", "k_value": "0.02"}
    unknown = client.put(f"{TYPES}/{made['id']}/k-values", json=[k])
    assert unknown.status_code == 422, unknown.text


SPOOLS = "/api/v1/filament/spools"


def _spool(client: TestClient, **over: object) -> dict[str, object]:
    made = client.post(SPOOLS, json={"material_id": _material_id(client)} | over)
    assert made.status_code == 201, made.text
    row: dict[str, object] = made.json()
    return row


def test_a_spool_is_made_read_changed_and_weighed(client: TestClient) -> None:
    made = _spool(client, grams_left=500, note="a")
    assert made["grams_left"] == 500
    one = f"{SPOOLS}/{made['id']}"
    assert client.get(one).json()["note"] == "a"
    patched = client.patch(one, json={"note": "b"})
    assert patched.status_code == 200, patched.text
    listed = client.get(SPOOLS, params={"needs_type": True}).json()
    assert [s["id"] for s in listed] == [made["id"]]
    assert [e["kind"] for e in client.get(f"{one}/events").json()] == ["created", "edited"]
    weighed = client.post(f"{one}/events", json={"kind": "weighed", "grams": 420})
    assert weighed.status_code == 201, weighed.text
    assert client.get(one).json()["grams_left"] == 420


def test_patching_a_colour_to_null_returns_the_spool_to_its_types_colour(
    client: TestClient,
) -> None:
    """Review focus 5, over HTTP: `rgba: null` clears the spool's own colour."""
    kind = client.post(
        "/api/v1/filament/types",
        json={
            "code": "PLA-RED",
            "material_id": _material_id(client),
            "rgba": "#FF0000",
            "label_weight_g": 1000,
        },
    )
    assert kind.status_code == 201, kind.text
    made = client.post(SPOOLS, json={"type_id": kind.json()["id"], "rgba": "#00FF00"})
    assert made.status_code == 201, made.text
    one = f"{SPOOLS}/{made.json()['id']}"
    assert made.json()["effective"]["rgba"] == "#00FF00FF"

    reset = client.patch(one, json={"rgba": None})

    assert reset.status_code == 200, reset.text
    assert reset.json()["effective"]["rgba"] == "#FF0000FF"
    assert "rgba" not in reset.json()["overridden"]
    events = client.get(f"{one}/events").json()
    assert [e["kind"] for e in events] == ["created", "edited"]
    assert events[-1]["data"] == {"changes": {"rgba": ["#00FF00FF", None]}}


def test_a_spool_that_is_typeless_or_backwards_is_422(client: TestClient) -> None:
    assert client.post(SPOOLS, json={"note": "x"}).status_code == 422
    one = f"{SPOOLS}/{_spool(client)['id']}"
    typeless = client.patch(one, json={"material_id": None})
    assert typeless.status_code == 422, typeless.text
    backwards = client.patch(one, json={"bed_temp_min": 90, "bed_temp_max": 60})
    assert backwards.status_code == 422, backwards.text


def test_a_bambuddy_linked_spool_is_not_deleted(client: TestClient, pg_conninfo: str) -> None:
    made = _spool(client)
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, bb_updated_at, linked_at)"
            " VALUES (%s, 7, now(), now())",
            (made["id"],),
        )
    one = f"{SPOOLS}/{made['id']}"
    refused = client.delete(one)
    assert refused.status_code == 409, refused.text
    assert "Bambuddy" in refused.json()["detail"]
    assert client.get(one).status_code == 200


def test_a_spool_with_prints_is_not_deleted(client: TestClient, pg_conninfo: str) -> None:
    made = _spool(client)
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO filament_spool_events (spool_id, kind, source, delta_g)"
            " VALUES (%s, 'print_ended', 'print_actual', -3)",
            (made["id"],),
        )
    one = f"{SPOOLS}/{made['id']}"
    refused = client.delete(one)
    assert refused.status_code == 409, refused.text
    assert client.get(one).status_code == 200
    archived = client.post(f"{one}/archive", json={"archived": True})
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None
    fresh = f"{SPOOLS}/{_spool(client)['id']}"
    assert client.delete(fresh).status_code == 204
    assert client.get(fresh).status_code == 404


def test_a_tag_is_bound_moved_and_unbound(client: TestClient) -> None:
    a, b = _spool(client), _spool(client)
    tag = {"tag_uid": "AABBCCDD", "kind": "bambu_mifare"}
    first = client.post(f"{SPOOLS}/{a['id']}/tags", json=tag)
    assert first.status_code == 201, first.text
    moved = client.post(f"{SPOOLS}/{b['id']}/tags", json=tag)
    assert moved.json()["tag_id"] == first.json()["tag_id"]
    binding = f"/api/v1/filament/tags/{first.json()['tag_id']}/binding"
    unbound = client.delete(binding)
    assert unbound.status_code == 200, unbound.text
    assert client.delete(binding).status_code == 404
    assert client.post(f"{SPOOLS}/{a['id']}/tags", json={"kind": "other"}).status_code == 422


def test_the_reads_the_filament_page_needs(client: TestClient) -> None:
    a = _spool(client, grams_left=400, label_weight_g=1000)
    tag = {"tag_uid": "AABBCCDD", "kind": "bambu_mifare"}
    assert client.post(f"{SPOOLS}/{a['id']}/tags", json=tag).status_code == 201
    detail = client.get(f"{SPOOLS}/{a['id']}").json()
    assert detail["tag"]["tag_uid"] == "AABBCCDD"
    assert detail["names"]["material"] == "PLA"
    assert detail["basis"]["anchor"]["grams_after"] == 400
    assert detail["in_use"] is False
    assert detail["low_stock"] is False
    rows = client.get("/api/v1/filament/tags").json()
    assert [r["spool_number"] for r in rows] == [detail["number"]]
    one = client.get(f"/api/v1/filament/tags/{rows[0]['id']}")
    assert one.status_code == 200, one.text
    assert [h["spool_id"] for h in one.json()["history"]] == [a["id"]]
    assert client.get(f"/api/v1/filament/tags/{MISSING}").status_code == 404
    page = client.get("/api/v1/filament/events", params={"limit": 1}).json()
    assert len(page["events"]) == 1
    assert page["events"][0]["spool_number"] == detail["number"]
    rest = client.get("/api/v1/filament/events", params={"cursor": page["next_cursor"]})
    assert rest.status_code == 200, rest.text
    assert client.get("/api/v1/filament/events", params={"cursor": "nope"}).status_code == 422
    assert client.get("/api/v1/filament/events", params={"limit": 501}).status_code == 422
    summary = client.get("/api/v1/filament/summary").json()
    assert summary["spools"] == 1
    assert summary["grams_left"] == 400


def test_a_spool_has_its_own_presets_and_k_values(client: TestClient) -> None:
    one = f"{SPOOLS}/{_spool(client)['id']}"
    rows = [{"printer_model": "H2C", "nozzle_diameter": "0.4", "slicer_filament": "P1"}]
    saved = client.put(f"{one}/presets", json=rows)
    assert saved.status_code == 200, saved.text
    assert saved.json()[0]["overridden"] is True
    assert client.get(f"{one}/presets").json() == saved.json()
    assert client.put(f"{one}/presets", json=[rows[0], rows[0]]).status_code == 422
    reset = client.put(f"{one}/presets", json=[])
    assert reset.status_code == 200
    assert reset.json() == []
    k = {"printer_id": MISSING, "extruder": 0, "nozzle_diameter": "0.4", "k_value": "0.02"}
    assert client.put(f"{one}/k-values", json=[k]).status_code == 422
    assert client.put(f"{one}/k-values", json=[]).json() == []
    assert client.get(f"{one}/k-values").json() == []


def test_a_k_value_is_set_and_read_back(client: TestClient, pg_conninfo: str) -> None:
    one = f"{SPOOLS}/{_spool(client)['id']}"
    printer = bambuddy_printer(pg_conninfo, 1)
    k = {"printer_id": printer, "extruder": 0, "nozzle_diameter": "0.4", "k_value": "0.02"}
    saved = client.put(f"{one}/k-values", json=[k])
    assert saved.status_code == 200, saved.text
    assert [(r["printer_id"], r["extruder"], r["overridden"]) for r in saved.json()] == [
        (printer, 0, True)
    ]
    assert client.get(f"{one}/k-values").json() == saved.json()
    assert client.put(f"{one}/k-values", json=[k, k]).status_code == 422


def test_a_missing_spool_is_404(client: TestClient) -> None:
    one = f"{SPOOLS}/{MISSING}"
    for leaf in ("presets", "k-values"):
        assert client.get(f"{one}/{leaf}").status_code == 404
        assert client.put(f"{one}/{leaf}", json=[]).status_code == 404
    assert client.get(one).status_code == 404
    assert client.patch(one, json={"note": "x"}).status_code == 404
    assert client.delete(one).status_code == 404
    assert client.post(f"{one}/archive", json={"archived": True}).status_code == 404
    assert client.get(f"{one}/events").status_code == 404
    assert client.post(f"{one}/events", json={"kind": "weighed", "grams": 1}).status_code == 404
    tag = {"tag_uid": "AA", "kind": "other"}
    assert client.post(f"{one}/tags", json=tag).status_code == 404
    assert client.post(f"{one}/move", json={"location_id": None}).status_code == 404


LOCATIONS = "/api/v1/filament/locations"


def _kind_id(client: TestClient, name: str = "Shelf") -> str:
    rows = client.get(f"{URL}/location-kinds").json()
    found: str = next(row["id"] for row in rows if row["name"] == name)
    return found


def _location(client: TestClient, name: str, **over: object) -> dict[str, object]:
    made = client.post(LOCATIONS, json={"name": name, "kind_id": _kind_id(client)} | over)
    assert made.status_code == 201, made.text
    row: dict[str, object] = made.json()
    return row


def test_locations_nest_and_a_move_is_answered_with_the_spool(client: TestClient) -> None:
    room = _location(client, "Room")
    shelf = _location(client, "Shelf A", parent_id=room["id"])
    spool = _spool(client)
    moved = client.post(f"{SPOOLS}/{spool['id']}/move", json={"location_id": shelf["id"]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["location_id"] == shelf["id"]
    tree = client.get(LOCATIONS).json()
    assert [n["name"] for n in tree] == ["Room"]
    assert [(n["name"], n["spool_count"]) for n in tree[0]["children"]] == [("Shelf A", 1)]
    again = client.post(f"{SPOOLS}/{spool['id']}/move", json={"location_id": shelf["id"]})
    assert again.status_code == 200
    events = client.get(f"{SPOOLS}/{spool['id']}/events").json()
    assert [e["kind"] for e in events] == ["created", "moved"]
    assert events[1]["data"] == {"from": None, "to": shelf["id"]}


def test_a_spool_moves_only_through_move(client: TestClient) -> None:
    place = _location(client, "A")
    spool = _spool(client)
    patched = client.patch(f"{SPOOLS}/{spool['id']}", json={"location_id": place["id"]})
    assert patched.status_code == 422, patched.text
    made = client.post(
        SPOOLS, json={"material_id": _material_id(client), "location_id": place["id"]}
    )
    assert made.status_code == 422, made.text


def test_a_location_cycle_or_clash_is_409_and_a_bad_move_is_422(client: TestClient) -> None:
    a = _location(client, "A")
    b = _location(client, "B", parent_id=a["id"])
    cycle = client.patch(f"{LOCATIONS}/{a['id']}", json={"parent_id": b["id"]})
    assert cycle.status_code == 409, cycle.text
    clash = client.post(LOCATIONS, json={"name": "a", "kind_id": _kind_id(client)})
    assert clash.status_code == 409, clash.text
    spool = _spool(client)
    gone = client.post(f"{SPOOLS}/{spool['id']}/move", json={"location_id": MISSING})
    assert gone.status_code == 422, gone.text
    archived = client.patch(f"{LOCATIONS}/{b['id']}", json={"archived": True})
    assert archived.json()["archived_at"] is not None
    refused = client.post(f"{SPOOLS}/{spool['id']}/move", json={"location_id": b["id"]})
    assert refused.status_code == 422, refused.text
    assert client.patch(f"{LOCATIONS}/{MISSING}", json={"note": "x"}).status_code == 404
    bad_kind = client.post(LOCATIONS, json={"name": "Z", "kind_id": MISSING})
    assert bad_kind.status_code == 422, bad_kind.text
    assert [n["name"] for n in client.get(LOCATIONS, params={"include_archived": True}).json()] == [
        "A"
    ]


def test_one_person_is_one_actor_by_session_or_token() -> None:
    now = datetime.now(UTC)
    principal = "user:https://id.example#ann"
    session = AuthSession(
        id_hash=b"x",
        principal=principal,
        issuer="https://id.example",
        subject="ann",
        name="Ann",
        email="ann@example.com",
        kind="login",
        created_at=now,
        last_seen_at=now,
        idle_expires_at=now,
        absolute_expires_at=now,
    )
    assert actor(Person(session)) == principal
    assert actor(PersonalToken(token_id="t", principal=principal, scope="write")) == principal
    assert actor(Service(caller="agent", scopes=frozenset())) == "service:agent"
    assert actor(ANONYMOUS) == "anonymous"


IMPORT_BB = "/api/v1/filament/bambuddy/import"


def test_the_bambuddy_import_needs_bambuddy(client: TestClient) -> None:
    refused = client.post(IMPORT_BB)
    assert refused.status_code == 409, refused.text


@respx.mock
def test_the_bambuddy_import_pulls_once_then_changes_nothing(client: TestClient) -> None:
    configure(client)
    bb = {
        "id": 41,
        "material": "PLA",
        "rgba": "00FF00",
        "label_weight": 1000,
        "weight_used": 250.0,
        "updated_at": "2026-11-14T10:00:00",
    }
    route = respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[bb]))
    respx.get(f"{API}/inventory/catalog").mock(return_value=httpx.Response(200, json=[]))
    first = client.post(IMPORT_BB)
    assert first.status_code == 200, first.text
    report = first.json()
    assert (report["created"], report["needs_type"]) == (1, 1)
    (spool_id,) = report["created_ids"]
    spool = client.get(f"/api/v1/filament/spools/{spool_id}").json()
    assert (spool["grams_left"], spool["rgba"]) == (750, "#00FF00FF")
    # A person's import names the caller, not the Schedule's "bambuddy".
    events = client.get(f"/api/v1/filament/spools/{spool_id}/events").json()
    assert {e["actor"] for e in events} == {actor(ANONYMOUS)}
    again = client.post(IMPORT_BB).json()
    assert (again["created"], again["linked"], again["updated"]) == (0, 0, 0)
    assert route.call_count == 2


# -- the inventory and a spool's summary (#6262 Task 9, F16) ---------------------------

INVENTORY = "/api/v1/filament/inventory"
TRAY_UUID = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


def _garage_reports_tray(client: TestClient, pg_conninfo: str) -> str:
    """A registry printer whose latest report shows AMS 0 tray 2 with a tagged PLA."""
    printer = str(uuid4())
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name, lan_host) VALUES (%s, %s, %s, %s)",
            (printer, "SN-GARAGE", "Garage", "10.0.0.9"),
        )
    tray = {"id": "2", "tray_type": "PLA", "tray_color": "FFFFFFFF", "state": 11}
    report = {"ams": {"ams": [{"id": "0", "tray": [tray | {"tray_uuid": TRAY_UUID}]}]}}
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    PrinterReportStore(state.settings_store.pool).put(printer, report, datetime.now(UTC))
    return printer


def test_inventory_route_lists_sb_spools(client: TestClient, pg_conninfo: str) -> None:
    """Every SB spool by number with its ``#RRGGBBAA`` and tray, and each registry
    printer's reported trays with the SB spool each holds. With no Bambuddy linked, the
    reports alone place the spools."""
    white = _spool(client, rgba="#FFFFFF", grams_left=500)
    clear = _spool(client, rgba="#FFFFFF80", grams_left=300)
    gone = _spool(client, rgba="#000000")
    assert client.post(f"{SPOOLS}/{gone['id']}/archive", json={"archived": True}).is_success
    tag = {"tray_uuid": TRAY_UUID, "kind": "bambu_mifare"}
    assert client.post(f"{SPOOLS}/{white['id']}/tags", json=tag).status_code == 201
    printer = _garage_reports_tray(client, pg_conninfo)

    response = client.get(INVENTORY)

    assert response.status_code == 200, response.text
    view = response.json()
    spools = {row["spool"]["id"]: row for row in view["spools"]}
    assert set(spools) == {white["id"], clear["id"]}
    assert spools[clear["id"]]["spool"]["effective"]["rgba"] == "#FFFFFF80"
    assert spools[clear["id"]]["loaded"] is None
    assert spools[white["id"]]["loaded"] == {
        "printer_id": printer,
        "bambuddy_printer_id": None,
        "printer_name": "Garage",
        "ams_id": 0,
        "tray_id": 2,
    }
    assert view["trays"] == [
        {
            "printer_id": printer,
            "printer_name": "Garage",
            "trays": [
                {
                    "ams_id": 0,
                    "tray_id": 2,
                    "label": "AMS-A slot 3",
                    "material": "PLA",
                    "rgba": "#FFFFFFFF",
                    "spool_id": white["id"],
                    "spool_number": white["number"],
                    "found_by": "tag",
                }
            ],
        }
    ]
    every = client.get(INVENTORY, params={"include_archived": True}).json()
    assert {row["spool"]["id"] for row in every["spools"]} == {
        white["id"],
        clear["id"],
        gone["id"],
    }


@respx.mock
def test_inventory_places_a_linked_spool_by_bambuddys_assignment(
    client: TestClient, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 9 review ruling (plan Decision 3, until #6263): while Bambuddy is linked, a
    spool no registry printer reports is in the tray Bambuddy assigns its linked Bambuddy
    spool to; a failed read places the spools by the reports alone."""
    configure(client)
    printer = bambuddy_printer(pg_conninfo, 2)
    bb = {
        "id": 15,
        "material": "PLA",
        "rgba": "00FF00FF",
        "label_weight": 1000,
        "weight_used": 250,
        "archived_at": None,
        "updated_at": "2026-11-15T00:00:00Z",
    }
    seed_app_sb_spools(client, [bb], offset=100)
    place = {"id": 1, "spool_id": 15, "printer_id": 2, "ams_id": 0, "tray_id": 1}
    assignments = respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=[place | {"printer_name": "Workshop"}])
    )

    [row] = client.get(INVENTORY).json()["spools"]

    assert (row["spool"]["number"], row["bb_spool_id"]) == (115, 15)
    assert row["loaded"] == {
        "printer_id": printer,
        "bambuddy_printer_id": 2,
        "printer_name": "Workshop",
        "ams_id": 0,
        "tray_id": 1,
    }
    summary = client.get(f"{SPOOLS}/{row['spool']['id']}/summary").json()
    assert summary["spool"]["loaded"]["printer_id"] == printer

    assignments.mock(return_value=httpx.Response(403, json={"detail": "no"}))
    monkeypatch.setattr(filament_api, "_assignments_seen", None)
    refused = client.get(INVENTORY)
    assert refused.status_code == 200, refused.text
    assert refused.json()["spools"][0]["loaded"] is None


@respx.mock
def test_a_slow_or_garbled_assignment_read_places_by_the_reports_alone(
    client: TestClient, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 2 (I1, M2): an assignment read that does not answer within
    `ASSIGNMENTS_TIMEOUT`, or answers 200 with no JSON, places by the reports alone."""
    configure(client)
    bambuddy_printer(pg_conninfo, 2)
    bb = {
        "id": 15,
        "material": "PLA",
        "rgba": "00FF00FF",
        "label_weight": 1000,
        "weight_used": 250,
        "archived_at": None,
        "updated_at": "2026-11-15T00:00:00Z",
    }
    seed_app_sb_spools(client, [bb], offset=100)
    route = respx.get(f"{API}/inventory/assignments")
    route.mock(return_value=httpx.Response(200, text="<html>not json</html>"))
    garbled = client.get(INVENTORY)
    assert garbled.status_code == 200, garbled.text
    assert garbled.json()["spools"][0]["loaded"] is None

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json=[])

    monkeypatch.setattr("scadbuddy.filament.bambuddy_pull.ASSIGNMENTS_TIMEOUT", 0.3)
    route.mock(side_effect=hang)
    monkeypatch.setattr(filament_api, "_assignments_seen", None)
    started = time.monotonic()
    slow = client.get(INVENTORY)
    assert time.monotonic() - started < 10  # far under the 30 s hang
    assert slow.status_code == 200, slow.text
    assert slow.json()["spools"][0]["loaded"] is None


@respx.mock
def test_the_assignments_are_cached_and_a_failure_is_not_retried_for_a_while(
    client: TestClient, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-1 of the final review: the inventory reads Bambuddy's assignments at most once
    in 5 s, and after a failed read not again for 30 s (served as no assignments)."""
    configure(client)
    bambuddy_printer(pg_conninfo, 2)
    bb = {
        "id": 15,
        "material": "PLA",
        "rgba": "00FF00FF",
        "label_weight": 1000,
        "weight_used": 250,
        "archived_at": None,
        "updated_at": "2026-11-15T00:00:00Z",
    }
    seed_app_sb_spools(client, [bb], offset=100)
    clock = [1000.0]
    monkeypatch.setattr(filament_api, "_now", lambda: clock[0])
    place = {"id": 1, "spool_id": 15, "printer_id": 2, "ams_id": 0, "tray_id": 1}
    route = respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=[place | {"printer_name": "Workshop"}])
    )

    def loaded() -> object:
        return client.get(INVENTORY).json()["spools"][0]["loaded"]

    assert loaded() is not None
    assert loaded() is not None
    spool_id = client.get(INVENTORY).json()["spools"][0]["spool"]["id"]
    assert client.get(f"{SPOOLS}/{spool_id}/summary").status_code == 200
    assert route.call_count == 1
    clock[0] += 4.9
    assert loaded() is not None
    assert route.call_count == 1
    clock[0] += 0.2
    route.mock(return_value=httpx.Response(503, json={"detail": "down"}))
    assert loaded() is None  # the fresh window had ended: this read fails
    assert route.call_count == 2
    assert loaded() is None  # and now Bambuddy is not asked
    assert route.call_count == 2
    clock[0] += 29.0
    assert loaded() is None
    assert route.call_count == 2
    clock[0] += 1.5
    route.mock(return_value=httpx.Response(200, json=[place | {"printer_name": "Workshop"}]))
    assert loaded() is not None
    assert route.call_count == 3


def test_a_settings_read_that_fails_is_a_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: the inventory and a spool's summary answer 503 when the settings cannot be
    read, as when the spools cannot."""
    made = _spool(client)

    def down(self: object) -> None:
        raise psycopg.OperationalError("the database is not answering")

    monkeypatch.setattr(SettingsStore, "load", down)
    for url in (INVENTORY, f"{SPOOLS}/{made['id']}/summary"):
        response = client.get(url)
        assert response.status_code == 503, (url, response.text)


def test_a_spool_summary_prices_it_and_counts_its_filament(client: TestClient) -> None:
    """What a filament hover card shows: grams left, cost per kg, the value left and a
    plate's cost, and the unarchived spools of the same filament. A translucent spool of
    the hue is another filament (#6316)."""
    one = _spool(client, rgba="#FFFFFF", grams_left=500, cost_per_kg="20")
    two = _spool(client, rgba="#ffffffff", grams_left=250)
    _spool(client, rgba="#FFFFFF80", grams_left=300)

    response = client.get(f"{SPOOLS}/{one['id']}/summary", params={"grams": 12.5})

    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["spool"]["spool"]["number"] == one["number"]
    assert (Decimal(summary["cost_per_kg"]), summary["price_per_g"]) == (Decimal(20), 0.02)
    assert (summary["value_left"], summary["grams"], summary["grams_cost"]) == (10.0, 12.5, 0.25)
    stock = summary["filament"]
    assert (stock["material"], stock["rgba"], stock["spool_count"]) == ("PLA", "#FFFFFFFF", 2)
    assert stock["spool_numbers"] == [one["number"], two["number"]]
    assert (stock["grams_left"], stock["loaded"]) == (750.0, [])
    unpriced = client.get(f"{SPOOLS}/{two['id']}/summary").json()
    assert (unpriced["cost_per_kg"], unpriced["value_left"], unpriced["grams_cost"]) == (
        None,
        None,
        None,
    )
    assert client.get(f"{SPOOLS}/{MISSING}/summary").status_code == 404


def test_an_archived_spool_is_summarised_but_not_counted_as_stock(client: TestClient) -> None:
    kept = _spool(client, rgba="#FFFFFF", grams_left=100)
    gone = _spool(client, rgba="#FFFFFF", grams_left=900)
    assert client.post(f"{SPOOLS}/{gone['id']}/archive", json={"archived": True}).is_success

    summary = client.get(f"{SPOOLS}/{gone['id']}/summary")

    assert summary.status_code == 200, summary.text
    stock = summary.json()["filament"]
    assert (stock["spool_numbers"], stock["grams_left"]) == ([kept["number"]], 100.0)


def test_the_price_per_gram_is_rounded_to_six_places(client: TestClient) -> None:
    """#2918: 15.96 per kg is 0.01596 per gram, not a float's tail; no grams of a priced
    spool cost 0.0, not null."""
    made = _spool(client, grams_left=400, cost_per_kg="15.96")

    body = client.get(f"{SPOOLS}/{made['id']}/summary", params={"grams": 0}).json()

    assert repr(body["price_per_g"]) == "0.01596"
    assert (body["grams"], body["grams_cost"]) == (0.0, 0.0)
    assert body["value_left"] == 6.384


def test_a_loaded_spools_summary_says_where_it_is_loaded(
    client: TestClient, pg_conninfo: str
) -> None:
    made = _spool(client, rgba="#FFFFFF", grams_left=500)
    tag = {"tray_uuid": TRAY_UUID, "kind": "bambu_mifare"}
    assert client.post(f"{SPOOLS}/{made['id']}/tags", json=tag).status_code == 201
    printer = _garage_reports_tray(client, pg_conninfo)

    summary = client.get(f"{SPOOLS}/{made['id']}/summary").json()

    [place] = summary["filament"]["loaded"]
    assert place["spool_id"] == made["id"]
    assert (place["loaded"]["printer_id"], place["loaded"]["tray_id"]) == (printer, 2)


def test_a_naive_since_or_until_is_422(client: TestClient) -> None:
    """The ledger's time filters take an aware datetime; a naive one is refused."""
    for name in ("since", "until"):
        refused = client.get("/api/v1/filament/events", params={name: "2026-01-01T00:00:00"})
        assert refused.status_code == 422, (name, refused.text)
    aware = client.get("/api/v1/filament/events", params={"until": "2026-01-01T00:00:00Z"})
    assert aware.status_code == 200, aware.text


# -- the CSV import (#6262 Task 15, F24) ------------------------------------------------

IMPORT = "/api/v1/filament/spools/import"


def test_an_unknown_number_is_an_error_and_blocks_the_import(client: TestClient) -> None:
    rows = [{"material": "PLA"}, {"number": "999", "note": "x"}]
    preview = client.post(IMPORT, json={"rows": rows, "dry_run": True})
    assert preview.status_code == 200, preview.text
    assert [r["action"] for r in preview.json()["rows"]] == ["create", "error"]
    refused = client.post(
        IMPORT, json={"rows": rows, "dry_run": False}, headers={"Idempotency-Key": "k-1"}
    )
    assert refused.status_code == 422, refused.text
    assert "line 3: no spool #999" in refused.json()["detail"]
    assert client.get(SPOOLS).json() == []


def test_a_retried_import_writes_once(client: TestClient) -> None:
    body = {"rows": [{"material": "PLA", "brand": "Sunlu"}], "dry_run": False}
    unkeyed = client.post(IMPORT, json=body)
    assert unkeyed.status_code == 428, unkeyed.text
    first = client.post(IMPORT, json=body, headers={"Idempotency-Key": "k-2"})
    assert first.status_code == 200, first.text
    again = client.post(IMPORT, json=body, headers={"Idempotency-Key": "k-2"})
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert len(client.get(SPOOLS).json()) == 1
    other = client.post(
        IMPORT, json=body | {"rows": [{"material": "PETG"}]}, headers={"Idempotency-Key": "k-2"}
    )
    assert other.status_code == 422, other.text
