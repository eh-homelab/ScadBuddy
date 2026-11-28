"""`/api/v1/filament` (#6261): the filament catalogs, spool types and spools."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api import filament as filament_api
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.filament import actor
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import SESSION_COOKIE
from scadbuddy.auth.models import (
    ANONYMOUS,
    AuthSession,
    Person,
    PersonalToken,
    Service,
    principal_for,
)
from scadbuddy.auth.service_credentials import LEGACY_CALLERS, ServiceCredentialStore, TokenReader
from scadbuddy.core.settings import Settings
from scadbuddy.filament import assignments_cache, rfid_mode
from scadbuddy.filament.bb_pull_models import PullOutcome
from scadbuddy.filament.component import FILAMENT
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.main import create_app
from scadbuddy.printers.reports import PrinterReportStore
from tests.api.test_send import API, configure
from tests.support.printers import bambuddy_printer
from tests.support.sb_rows import seed_app_sb_spools

pytestmark = pytest.mark.requires_postgres

URL = "/api/v1/filament/catalog"


def _forget_assignments(client: TestClient) -> None:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    state.components.get(FILAMENT).assignments.seen = None


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


def _insert(client: TestClient) -> str:
    made = client.post(f"{URL}/insert-types", json={"name": "Plastic", "empty_weight_g": 200})
    assert made.status_code == 201, made.text
    insert: str = made.json()["id"]
    return insert


def test_a_weighing_with_a_reading_answers_its_breakdown(client: TestClient) -> None:
    """#7682: a reading alone takes the insert off, keeps the breakdown and is the detail's
    `last_weighing`."""
    made = _spool(client, label_weight_g=1000, insert_type_id=_insert(client))
    one = f"{SPOOLS}/{made['id']}"
    first = client.post(f"{one}/events", json={"kind": "weighed", "source": "scale", "grams": 1000})
    assert first.status_code == 201, first.text
    weighed = client.post(
        f"{one}/events", json={"kind": "weighed", "source": "scale", "reading_g": 1180}
    )
    assert weighed.status_code == 201, weighed.text
    assert weighed.json()["grams_after"] == 980
    data = weighed.json()["data"]
    assert data["drift_g"] == -20.0 and data["expected_g"] == 1000
    assert (data["empty_g"], data["empty_source"], data["empty_name"]) == (200, "insert", "Plastic")
    spool = client.get(one).json()
    assert spool["last_weighing"]["id"] == weighed.json()["id"]
    assert spool["last_drift_g"] == -20.0 and spool["drift_notice"] is True


def test_a_reading_below_the_empty_weight_is_refused(client: TestClient) -> None:
    made = _spool(client, label_weight_g=1000, insert_type_id=_insert(client))
    one = f"{SPOOLS}/{made['id']}"
    refused = client.post(f"{one}/events", json={"kind": "weighed", "reading_g": 150})
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "reading_below_empty"
    assert [e["kind"] for e in client.get(f"{one}/events").json()] == ["created"]


def test_a_reading_alone_needs_an_empty_weight(client: TestClient) -> None:
    made = _spool(client, label_weight_g=1000)
    one = f"{SPOOLS}/{made['id']}"
    refused = client.post(f"{one}/events", json={"kind": "weighed", "reading_g": 1180})
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "no_empty_weight"
    assert [e["kind"] for e in client.get(f"{one}/events").json()] == ["created"]


def test_the_closing_preview_is_read_under_the_spool(client: TestClient) -> None:
    """#7684: the archive dialog's preview of a spool's closing numbers and notices."""
    made = _spool(client, label_weight_g=1000, insert_type_id=_insert(client))
    one = f"{SPOOLS}/{made['id']}"
    first = client.post(f"{one}/events", json={"kind": "weighed", "source": "scale", "grams": 1000})
    assert first.status_code == 201, first.text
    preview = client.get(f"{one}/closing", params={"reason": "finished", "final_reading_g": 230})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["final_filament_g"] == 30 and body["empty_g"] == 200
    assert body["closing"]["started_g"] == 1000 and body["closing"]["scale_consumed_g"] == 970
    assert [n["code"] for n in body["notices"]] == ["filament_left"]
    assert (
        client.get(f"{SPOOLS}/{uuid4()}/closing", params={"reason": "finished"}).status_code == 404
    )
    assert client.get(f"{one}/closing", params={"reason": "lost"}).status_code == 422
    assert [e["kind"] for e in client.get(f"{one}/events").json()] == ["created", "weighed"]


def test_an_archive_takes_a_reason_and_a_final_reading(client: TestClient) -> None:
    """#7684: the final weighing is booked, then the `archived` event with the closing."""
    made = _spool(client, label_weight_g=1000, insert_type_id=_insert(client))
    one = f"{SPOOLS}/{made['id']}"
    first = client.post(f"{one}/events", json={"kind": "weighed", "source": "scale", "grams": 1000})
    assert first.status_code == 201, first.text
    done = client.post(
        f"{one}/archive", json={"archived": True, "reason": "finished", "final_reading_g": 230}
    )
    assert done.status_code == 200, done.text
    assert done.json()["archived_at"] is not None and done.json()["grams_left"] == 0
    events = client.get(f"{one}/events").json()
    assert [e["kind"] for e in events] == [
        "created",
        "weighed",
        "weighed",
        "non_print_waste",
        "archived",
    ]
    assert events[-1]["data"]["reason"] == "finished"
    assert events[-1]["data"]["scale_consumed_g"] == 970 and events[-1]["data"]["drift_g"] == -970
    refused = client.post(f"{one}/archive", json={"archived": False, "reason": "finished"})
    assert refused.status_code == 422, refused.text


def test_an_archive_with_a_reading_under_the_empty_weight_is_refused(client: TestClient) -> None:
    made = _spool(client, label_weight_g=1000, insert_type_id=_insert(client))
    one = f"{SPOOLS}/{made['id']}"
    refused = client.post(f"{one}/archive", json={"reason": "damaged", "final_reading_g": 150})
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "reading_below_empty"
    assert client.get(one).json()["archived_at"] is None
    assert [e["kind"] for e in client.get(f"{one}/events").json()] == ["created"]
    bare = _spool(client, label_weight_g=1000)
    refused = client.post(f"{SPOOLS}/{bare['id']}/archive", json={"final_reading_g": 230})
    assert refused.status_code == 422 and refused.json()["code"] == "no_empty_weight"


def _new_spool(client: TestClient) -> dict[str, object]:
    """New by #7280's rule: 1000 g left of a 1000 g label, nothing consumed, on a 200 g insert."""
    return _spool(
        client,
        label_weight_g=1000,
        insert_type_id=_insert(client),
        grams_left=1000,
        added_full=True,
    )


def test_a_new_spool_under_its_label_takes_a_resolution(client: TestClient) -> None:
    """#7683: the choice the check offered rides on the weighing and stays in its data."""
    made = _new_spool(client)
    one = f"{SPOOLS}/{made['id']}"
    check = client.get(f"{one}/insert-weight-offer", params={"reading_g": 900}).json()
    assert check["reason"] == "below_label" and check["default"] == "tared"
    weighed = client.post(
        f"{one}/events",
        json={"kind": "weighed", "source": "scale", "reading_g": 900, "resolution": "tared"},
    )
    assert weighed.status_code == 201, weighed.text
    assert weighed.json()["grams_after"] == 900
    data = weighed.json()["data"]
    assert (data["resolution"], data["shortfall_g"]) == ("tared", 100)
    assert data["empty_source"] == "tared"


def test_a_resolution_on_a_used_spool_is_refused(client: TestClient) -> None:
    made = _new_spool(client)
    one = f"{SPOOLS}/{made['id']}"
    used = client.post(f"{one}/events", json={"kind": "weighed", "grams": 900})
    assert used.status_code == 201, used.text
    refused = client.post(
        f"{one}/events", json={"kind": "weighed", "reading_g": 900, "resolution": "tared"}
    )
    assert refused.status_code == 422, refused.text
    assert (refused.json()["code"], refused.json()["reason"]) == ("no_below_label", "used")
    assert [e["kind"] for e in client.get(f"{one}/events").json()] == ["created", "weighed"]


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
    assert detail["tag"]["tag_uid"] == "AABBCCDD00000100"
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


PUBLIC = "https://scadbuddy.test"


def _observed(settings: Settings, tmp_path: Path) -> TestClient:
    """An app in ``observe``, where a session names its person and the agent's credential
    may name whom it acts for (`test_authorship_session.py`)."""
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(os.urandom(32)).decode())
    app = create_app(
        settings.model_copy(
            update={"secret_key_file": key, "public_url": PUBLIC, "auth_mode": "observe"}
        )
    )
    return TestClient(app, base_url=PUBLIC, headers={"X-ScadBuddy-Request": "1"})


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def test_patch_spool_by_agent_records_session(settings: Settings, tmp_path: Path) -> None:
    with _observed(settings, tmp_path) as client:
        store = _state(client).settings_store
        if "agent" in LEGACY_CALLERS:
            token = TokenReader("agent", settings.database_url, store.kek).token()
        else:
            token = (
                ServiceCredentialStore(store.pool)
                .issue("agent", "bootstrap", rate_limited=False)
                .token
            )
        assert token is not None
        agent = {
            "Authorization": f"Bearer {token}",
            "X-ScadBuddy-Agent-Author": "user:https://id.example#ann",
            "X-ScadBuddy-Agent-Author-Session": "s-1",
            "X-ScadBuddy-Agent-Tool": "filament_spool_upsert",
        }
        spool = _spool(client)
        patched = client.patch(f"{SPOOLS}/{spool['id']}", json={"note": "hi"}, headers=agent)
        assert patched.status_code == 200, patched.text
        edited = client.get(f"{SPOOLS}/{spool['id']}/events").json()[-1]
    assert edited["kind"] == "edited"
    assert edited["actor"] == "service:agent"
    assert edited["data"]["author"] == {
        "principal": "user:https://id.example#ann",
        "session": "s-1",
        "tool": "filament_spool_upsert",
    }


def test_patch_type_records_the_caller(settings: Settings, tmp_path: Path) -> None:
    with _observed(settings, tmp_path) as client:
        auth = _state(client).components.get(AUTH)
        cookie, _ = auth.sessions.create(issuer="https://idp.test", subject="bo", kind="login")
        client.cookies.set(SESSION_COOKIE, cookie)
        made = _type(client, cost_per_kg="20")
        spool = _spool(client, type_id=made["id"])
        patched = client.patch(f"{TYPES}/{made['id']}", json={"cost_per_kg": "25"})
        assert patched.status_code == 200, patched.text
        edited = client.get(f"{SPOOLS}/{spool['id']}/events").json()[-1]
    assert edited["kind"] == "edited"
    assert edited["actor"] == principal_for("https://idp.test", "bo")
    assert "author" not in edited["data"]


IMPORT_BB = "/api/v1/filament/bambuddy/import"


def test_the_bambuddy_import_needs_bambuddy(client: TestClient) -> None:
    refused = client.post(IMPORT_BB)
    assert refused.status_code == 409, refused.text


@respx.mock
def test_the_bambuddy_import_reads_the_weights_setting(client: TestClient) -> None:
    configure(client, filament_sync={"weights": "off"})
    bb = {
        "id": 41,
        "material": "PLA",
        "rgba": "00FF00",
        "label_weight": 1000,
        "weight_used": 250.0,
        "core_weight": 250,
        "last_scale_weight": 700,
        "last_weighed_at": "2026-11-14T10:00:00",
        "updated_at": "2026-11-14T10:00:00",
    }
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[bb]))
    respx.get(f"{API}/inventory/catalog").mock(return_value=httpx.Response(200, json=[]))
    (spool_id,) = client.post(IMPORT_BB).json()["created_ids"]
    events = client.get(f"/api/v1/filament/spools/{spool_id}/events").json()
    assert [e for e in events if e["kind"] == "weighed"] == []


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


def _kept_actors(pg_conninfo: str) -> list[tuple[str, str]]:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        return conn.execute("SELECT pull_id, actor FROM filament_pull_actors").fetchall()


@respx.mock
def test_a_finished_import_drops_its_kept_actor(client: TestClient, pg_conninfo: str) -> None:
    configure(client)
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/inventory/catalog").mock(return_value=httpx.Response(200, json=[]))
    assert client.post(IMPORT_BB).status_code == 200
    assert _kept_actors(pg_conninfo) == []


@pytest.mark.parametrize(
    "ended",
    [
        pytest.param(TimeoutError("the run timed out"), id="the-run-timed-out"),
        pytest.param(PullOutcome(outcome="failed"), id="the-read-failed-or-timed-out"),
    ],
)
def test_an_import_that_did_not_finish_keeps_its_actor_and_says_it_may_run_on(
    client: TestClient,
    pg_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
    ended: Exception | PullOutcome,
) -> None:
    """Decision 183: the route did not see the read end (the run timed out, Temporal out of
    reach, the read failed or timed out), so the worker may still be sending its bodies.
    The person stays kept under the run's id, for the bodies still to come to name them;
    a later `remember_actor` drops the row once it is `ACTOR_KEPT` old. The 503 says the
    import may still be running."""
    configure(client)

    async def unfinished(*args: object) -> PullOutcome:
        if isinstance(ended, Exception):
            raise ended
        return ended

    monkeypatch.setattr(filament_api, "execute_pull", unfinished)
    answer = client.post(IMPORT_BB)
    assert answer.status_code == 503, answer.text
    assert "may still be running" in answer.json()["detail"]
    # The page tells it apart from any other 503 by its code.
    assert answer.json()["code"] == "import_unfinished"
    ((pull_id, kept),) = _kept_actors(pg_conninfo)
    assert pull_id.startswith("filament-bb-pull-")
    assert kept == actor(ANONYMOUS)


# -- type proposals (#6701, spec §8.1) --------------------------------------------------

PROPOSALS = "/api/v1/filament/type-proposals"


def test_type_proposals_are_listed_and_confirmed_under_the_callers_name(
    client: TestClient,
) -> None:
    red = [_spool(client, rgba="#FF0000", label_weight_g=1000) for _ in range(2)]
    bare = _spool(client)
    listed = client.get(PROPOSALS)
    assert listed.status_code == 200, listed.text
    (group,) = listed.json()["groups"]
    assert (group["code"], group["count"], group["type_id"]) == ("PLA-FF0000", 2, None)
    assert sorted(group["spool_ids"]) == sorted(str(s["id"]) for s in red)
    assert listed.json()["ungrouped"] == [
        {"spool_id": bare["id"], "number": bare["number"], "missing": ["rgba", "label_weight_g"]}
    ]
    keys = ("brand_id", "material_id", "subtype_id", "rgba", "label_weight_g", "material_number")
    pick = {k: group[k] for k in keys} | {"code": "PLA Red", "spool_ids": group["spool_ids"]}
    done = client.post(f"{PROPOSALS}/confirm", json={"groups": [pick]})
    assert done.status_code == 200, done.text
    (result,) = done.json()["groups"]
    assert (result["code"], result["made"], result["skipped"]) == ("PLA Red", True, [])
    for spool in red:
        one = client.get(f"{SPOOLS}/{spool['id']}").json()
        assert one["type_id"] == result["type_id"]
        events = client.get(f"{SPOOLS}/{spool['id']}/events").json()
        assert [(e["kind"], e["actor"]) for e in events][-1] == ("edited", actor(ANONYMOUS))
    assert client.get(PROPOSALS).json()["groups"] == []


def test_a_type_proposal_confirm_with_a_taken_code_is_409(client: TestClient) -> None:
    _type(client, code="TAKEN", rgba="#0000FF")
    spool = _spool(client, rgba="#FF0000", label_weight_g=1000)
    (group,) = client.get(PROPOSALS).json()["groups"]
    keys = ("brand_id", "material_id", "subtype_id", "rgba", "label_weight_g", "material_number")
    pick = {k: group[k] for k in keys} | {"code": "taken", "spool_ids": [spool["id"]]}
    refused = client.post(f"{PROPOSALS}/confirm", json={"groups": [pick]})
    assert refused.status_code == 409, refused.text
    assert client.get(f"{SPOOLS}/{spool['id']}").json()["type_id"] is None
    empty = client.post(f"{PROPOSALS}/confirm", json={"groups": []})
    assert empty.status_code == 422, empty.text


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
                    "mismatch": None,
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
    _forget_assignments(client)
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
    _forget_assignments(client)
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
    monkeypatch.setattr(assignments_cache, "now", lambda: clock[0])
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


# -- location sensors (#6268) ----------------------------------------------------------

SENSORS = "/api/v1/filament/sensors"


def _sensor_body(client: TestClient, **over: object) -> dict[str, object]:
    place = _location(client, f"Box {uuid4().hex[:6]}")
    return {
        "location_id": place["id"],
        "source": "home_assistant",
        "kind": "humidity",
        "entity_id": "sensor.box",
        "name": "Box humidity",
        "unit": "%",
        **over,
    }


def test_a_sensor_is_made_listed_changed_and_archived(client: TestClient) -> None:
    body = _sensor_body(client)
    made = client.post(SENSORS, json=body)
    assert made.status_code == 201, made.text
    sensor = made.json()
    assert (sensor["name"], sensor["reachable"], sensor["last_value"]) == (
        "Box humidity",
        True,
        None,
    )
    assert [s["id"] for s in client.get(SENSORS).json()] == [sensor["id"]]
    assert client.get(SENSORS, params={"location_id": body["location_id"]}).json()
    assert client.get(SENSORS, params={"location_id": MISSING}).json() == []
    changed = client.patch(f"{SENSORS}/{sensor['id']}", json={"name": "Dry box", "alert_above": 55})
    assert changed.status_code == 200, changed.text
    assert (changed.json()["name"], changed.json()["alert_above"]) == ("Dry box", 55)
    assert client.post(f"{SENSORS}/{sensor['id']}/archive").status_code == 204
    assert client.get(SENSORS).json() == []
    assert client.post(f"{SENSORS}/{sensor['id']}/archive").status_code == 404
    assert client.patch(f"{SENSORS}/{sensor['id']}", json={"name": "x"}).status_code == 404


def test_a_sensor_by_hand_is_not_an_ams_one(client: TestClient) -> None:
    refused = client.post(
        SENSORS,
        json=_sensor_body(client, source="ams", entity_id=None, printer_id=MISSING, ams_id=0),
    )
    assert refused.status_code == 422, refused.text
    assert "ams" in refused.text.lower()


def test_a_bad_sensor_is_refused(client: TestClient) -> None:
    assert client.post(SENSORS, json=_sensor_body(client, entity_id=None)).status_code == 422
    nowhere = client.post(SENSORS, json=_sensor_body(client, location_id=MISSING))
    assert nowhere.status_code == 422, nowhere.text
    assert client.patch(f"{SENSORS}/{MISSING}", json={"name": ""}).status_code == 422


def test_a_sensors_readings_are_read_from_a_time(client: TestClient, pg_conninfo: str) -> None:
    sensor = client.post(SENSORS, json=_sensor_body(client)).json()
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        for minute, value in ((0, 40.0), (1, 41.0), (2, 42.0)):
            conn.execute(
                "INSERT INTO filament_sensor_readings (sensor_id, at, value) VALUES (%s, %s, %s)",
                (sensor["id"], datetime(2026, 11, 18, 12, minute, tzinfo=UTC), value),
            )
    url = f"{SENSORS}/{sensor['id']}/readings"
    got = client.get(url, params={"since": "2026-11-18T12:01:00Z"})
    assert got.status_code == 200, got.text
    assert [r["value"] for r in got.json()] == [41.0, 42.0]
    limited = client.get(url, params={"since": "2026-11-18T00:00:00Z", "limit": 2}).json()
    # With more rows than the limit, the newest come, still oldest first.
    assert [r["value"] for r in limited] == [41.0, 42.0]
    assert client.get(f"{SENSORS}/{MISSING}/readings").status_code == 404
    assert client.get(url, params={"limit": 0}).status_code == 422


def test_the_ha_entities_need_bambuddy(client: TestClient) -> None:
    refused = client.get(f"{SENSORS}/entities")
    assert refused.status_code == 409, refused.text
    assert "Bambuddy" in refused.text


@respx.mock
def test_the_ha_entities_are_proxied_and_not_taken_for_a_sensor(client: TestClient) -> None:
    configure(client)
    route = respx.get(f"{API}/location-ha-sensors/entities").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "entity_id": "sensor.box",
                    "friendly_name": "Box",
                    "state": "20",
                    "domain": "sensor",
                    "device_class": "humidity",
                    "unit_of_measurement": "%",
                }
            ],
        )
    )
    got = client.get(f"{SENSORS}/entities", params={"search": "box"})
    assert got.status_code == 200, got.text
    assert got.json()[0]["entity_id"] == "sensor.box"
    assert route.calls.last.request.url.params["search"] == "box"


def test_the_route_answers_422_for_an_override_on_an_ntag(client: TestClient) -> None:
    spool = _spool(client)
    body = {"tag_uid": "AABBCCDD", "kind": "ntag_opentag", "override": True}
    assert client.post(f"{SPOOLS}/{spool['id']}/tags", json=body).status_code == 422
    ok = client.post(
        f"{SPOOLS}/{spool['id']}/tags",
        json={"tray_uuid": "0123456789abcdef0123456789abcdef", "kind": "bambu_mifare"}
        | {"override": True},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["override"] is True


@respx.mock
def test_the_route_binds_even_when_bambuddy_is_down(client: TestClient, pg_conninfo: str) -> None:
    """#6264 Task 4: the tag is pushed once inline, and a Bambuddy that is down never fails
    the request; the row stays queued for the refresh."""
    configure(client, filament_sync={"tags": "sb_to_bb"})
    route = respx.get(f"{API}/inventory/spools/by-tag").mock(side_effect=httpx.ConnectError("down"))
    spool = _spool(client)

    bound = client.post(
        f"{SPOOLS}/{spool['id']}/tags", json={"tag_uid": "AABBCCDD", "kind": "ntag_opentag"}
    )

    assert bound.status_code == 201, bound.text
    assert route.call_count == 1
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT attempts FROM filament_bb_tag_pending").fetchone()
    assert row == (1,)
    assert client.get("/api/v1/filament/tags").json()[0]["tag_uid"] == "AABBCCDD00000100"


@respx.mock
def test_the_route_calls_bambuddy_not_at_all_with_tags_off(
    client: TestClient, pg_conninfo: str
) -> None:
    configure(client, filament_sync={"tags": "off"})
    spool = _spool(client)

    bound = client.post(
        f"{SPOOLS}/{spool['id']}/tags", json={"tag_uid": "AABBCCDD", "kind": "ntag_opentag"}
    )

    assert bound.status_code == 201, bound.text
    assert len(respx.calls) == 0
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT attempts FROM filament_bb_tag_pending").fetchone()
    assert row == (0,)


RFID = "/api/v1/filament/rfid"
SETTINGS_URL = f"{API}/settings/"


def _auto_add(*values: bool) -> respx.Route:
    return respx.get(SETTINGS_URL).mock(
        side_effect=[httpx.Response(200, json={"auto_add_unknown_rfid": v}) for v in values]
    )


@respx.mock
def test_the_mode_route_writes_the_row_then_turns_auto_add_off(client: TestClient) -> None:
    configure(client)
    _auto_add(True, False)
    patch = respx.patch(SETTINGS_URL).mock(return_value=httpx.Response(200, json={}))

    on = client.put(f"{RFID}/override-mode", json={"enabled": True})

    assert on.status_code == 200, on.text
    body = on.json()
    assert (body["override_mode"], body["state"]) == (True, "applied")
    assert body["saved_auto_add"] is True
    assert patch.call_count == 1


@respx.mock
def test_the_auto_add_setting_round_trips_and_holds_bambuddy_off_on_save(
    client: TestClient,
) -> None:
    """#7156 R-7156-1 to R-7156-4: stored only, on by default; a save that turns it off
    reconciles Bambuddy at once, and a save back on restores the saved value."""
    name = "filament_auto_add_unknown_rfid"
    configure(client)
    body = client.get("/api/v1/settings").json()
    assert body[name] is True
    assert name not in body["sources"]
    _auto_add(True, False, False)
    patch = respx.patch(SETTINGS_URL).mock(return_value=httpx.Response(200, json={}))

    assert client.put("/api/v1/settings", json={name: False}).status_code == 200

    assert client.get("/api/v1/settings").json()[name] is False
    assert [json.loads(c.request.content) for c in patch.calls] == [
        {"auto_add_unknown_rfid": False}
    ]
    status_now = client.get(f"{RFID}/status").json()
    assert (status_now["override_mode"], status_now["auto_add_unknown_rfid"]) == (False, False)
    assert (status_now["state"], status_now["saved_auto_add"]) == ("applied", True)
    _auto_add(False)

    assert client.put("/api/v1/settings", json={name: None}).status_code == 200

    assert client.get("/api/v1/settings").json()[name] is True
    assert json.loads(patch.calls[-1].request.content) == {"auto_add_unknown_rfid": True}


@respx.mock
def test_a_save_that_leaves_the_auto_add_setting_calls_no_bambuddy(client: TestClient) -> None:
    configure(client)

    assert client.put("/api/v1/settings", json={"filament_low_stock_pct": 30}).status_code == 200

    assert not [c for c in respx.calls if c.request.url.path.endswith("/settings/")]


@respx.mock
def test_a_save_of_the_auto_add_setting_unchanged_calls_no_bambuddy(client: TestClient) -> None:
    """#7156 review: only a change reconciles, so a save of the same value never waits on the
    mode's lock or calls Bambuddy."""
    name = "filament_auto_add_unknown_rfid"
    configure(client)
    reads = respx.get(SETTINGS_URL).mock(
        return_value=httpx.Response(200, json={"auto_add_unknown_rfid": False})
    )
    respx.patch(SETTINGS_URL).mock(return_value=httpx.Response(200, json={}))
    assert client.put("/api/v1/settings", json={name: False}).status_code == 200
    assert reads.call_count >= 1  # the change itself reconciled
    calls = len(respx.calls)

    assert client.put("/api/v1/settings", json={name: False}).status_code == 200
    assert (
        client.put("/api/v1/settings", json={name: False, "filament_low_stock_pct": 25}).status_code
        == 200
    )

    assert len(respx.calls) == calls


@respx.mock
def test_a_blocked_write_is_still_a_200(client: TestClient) -> None:
    configure(client)
    _auto_add(True)
    respx.patch(SETTINGS_URL).mock(return_value=httpx.Response(403, json={"detail": "Forbidden"}))

    blocked = client.put(f"{RFID}/override-mode", json={"enabled": True})

    assert blocked.status_code == 200, blocked.text
    body = blocked.json()
    assert body["state"] == "blocked"
    assert body["bambuddy_auto_add"] is True  # what Bambuddy answered, never "off"
    assert "auth is on" in body["detail"]
    assert body["override_mode"] is True
    assert body["saved_auto_add"] is True

    # The status route reports the refusal until a reconcile lands.
    respx.get(SETTINGS_URL).mock(
        return_value=httpx.Response(200, json={"auto_add_unknown_rfid": True})
    )
    status_now = client.get(f"{RFID}/status").json()
    assert status_now["state"] == "blocked"
    assert status_now["bambuddy_auto_add"] is True


@respx.mock
def test_bambuddy_down_leaves_the_mode_on_and_pending(client: TestClient) -> None:
    configure(client)
    respx.get(SETTINGS_URL).mock(side_effect=httpx.ConnectError("down"))

    pending = client.put(f"{RFID}/override-mode", json={"enabled": True})

    assert pending.status_code == 200, pending.text
    assert pending.json()["state"] == "pending"
    assert pending.json()["override_mode"] is True
    assert pending.json()["detail"]


@respx.mock
def test_the_status_route_reads_without_writing(client: TestClient) -> None:
    configure(client)
    _auto_add(True)
    patch = respx.patch(SETTINGS_URL).mock(return_value=httpx.Response(200, json={}))

    status = client.get(f"{RFID}/status")

    assert status.status_code == 200, status.text
    body = status.json()
    assert (body["state"], body["bambuddy_auto_add"]) == ("off", True)
    assert patch.call_count == 0


@respx.mock
def test_the_status_route_leaves_fields_empty_when_bambuddy_is_down(client: TestClient) -> None:
    configure(client)
    respx.get(SETTINGS_URL).mock(side_effect=httpx.ConnectError("down"))

    status = client.get(f"{RFID}/status")

    assert status.status_code == 200, status.text
    assert status.json()["bambuddy_auto_add"] is None


@respx.mock
def test_the_reconcile_route_runs_the_mode_and_the_tags(client: TestClient) -> None:
    configure(client, filament_sync={"tags": "sb_to_bb"})
    respx.get(SETTINGS_URL).mock(side_effect=httpx.ConnectError("down"))
    client.put(f"{RFID}/override-mode", json={"enabled": True})
    _auto_add(True, False)
    respx.patch(SETTINGS_URL).mock(return_value=httpx.Response(200, json={}))

    done = client.post(f"{RFID}/reconcile")

    assert done.status_code == 200, done.text
    body = done.json()
    assert body["mode"]["state"] == "applied"
    assert body["tags"] == {"pushed": 0, "moved": 0, "cleared": 0, "unchanged": 0, "failed": 0}
    assert body["slots"] == {
        "configured": 0,
        "unconfirmed": 0,
        "waiting": 0,
        "unchanged": 0,
        "no_preset": 0,
        "no_temps": 0,
        "invalid_temps": 0,
        "no_nozzle": 0,
        "unsupported_unit": 0,
        "failed": 0,
    }


@respx.mock
def test_a_failing_reconcile_step_leaves_the_others_to_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(client, filament_sync={"tags": "sb_to_bb"})

    async def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("the mode step broke")

    monkeypatch.setattr("scadbuddy.api.filament.reconcile_mode_now", broken)

    done = client.post(f"{RFID}/reconcile")

    assert done.status_code == 200, done.text
    body = done.json()
    assert (body["mode"]["state"], body["mode"]["detail"]) == ("pending", "internal")
    assert body["tags"] == {"pushed": 0, "moved": 0, "cleared": 0, "unchanged": 0, "failed": 0}
    assert body["slots"] == {
        "configured": 0,
        "unconfirmed": 0,
        "waiting": 0,
        "unchanged": 0,
        "no_preset": 0,
        "no_temps": 0,
        "invalid_temps": 0,
        "no_nozzle": 0,
        "unsupported_unit": 0,
        "failed": 0,
    }


@respx.mock
def test_the_mode_route_answers_busy_while_another_reconcile_holds_the_lock(
    client: TestClient, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(client)
    monkeypatch.setattr(rfid_mode, "RFID_MODE_LOCK_WAIT_SECONDS", 0.3)
    holder = psycopg.connect(pg_conninfo, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (rfid_mode.RFID_MODE_LOCK,))
    try:
        began = time.monotonic()
        busy = client.put(f"{RFID}/override-mode", json={"enabled": True})
        waited = time.monotonic() - began
    finally:
        holder.close()

    assert busy.status_code == 200, busy.text
    assert (busy.json()["state"], busy.json()["detail"]) == ("pending", "busy")
    assert busy.json()["override_mode"] is True  # the row was written
    assert waited < 3
    assert len(respx.calls) == 0


@respx.mock
def test_unbinding_a_tag_pushes_the_clear_at_once_with_tags_sb_to_bb(
    client: TestClient, pg_conninfo: str
) -> None:
    configure(client, filament_sync={"tags": "sb_to_bb"})
    route = respx.get(f"{API}/inventory/spools/by-tag").mock(side_effect=httpx.ConnectError("down"))
    spool = _spool(client)
    bound = client.post(
        f"{SPOOLS}/{spool['id']}/tags", json={"tag_uid": "AABBCCDD", "kind": "ntag_opentag"}
    )
    assert bound.status_code == 201, bound.text
    calls = route.call_count

    closed = client.delete(f"/api/v1/filament/tags/{bound.json()['tag_id']}/binding")

    assert closed.status_code == 200, closed.text
    assert route.call_count == calls + 1


def test_a_patch_that_inverts_the_stored_band_is_refused(client: TestClient) -> None:
    made = client.post(SENSORS, json=_sensor_body(client, alert_above=60)).json()
    refused = client.patch(f"{SENSORS}/{made['id']}", json={"alert_below": 80})
    assert refused.status_code == 422, refused.text
    assert "alert_below must be under alert_above" in refused.text
