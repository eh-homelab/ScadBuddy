"""`/api/v1/filament` (#6261): the filament catalogs, spool types and spools."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.filament import actor
from scadbuddy.auth.models import ANONYMOUS, AuthSession, Person, PersonalToken, Service
from tests.api.test_send import API, configure

pytestmark = pytest.mark.requires_postgres

URL = "/api/v1/filament/catalog"
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


def test_a_spool_that_is_typeless_or_backwards_is_422(client: TestClient) -> None:
    assert client.post(SPOOLS, json={"note": "x"}).status_code == 422
    one = f"{SPOOLS}/{_spool(client)['id']}"
    typeless = client.patch(one, json={"material_id": None})
    assert typeless.status_code == 422, typeless.text
    backwards = client.patch(one, json={"bed_temp_min": 90, "bed_temp_max": 60})
    assert backwards.status_code == 422, backwards.text


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


def test_a_missing_spool_is_404(client: TestClient) -> None:
    one = f"{SPOOLS}/{MISSING}"
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
