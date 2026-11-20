"""`POST /filament/types/{id}/fill-from-profile` and `POST /filament/types/fill-from-profile`
(#6714), and the fill after a type is made (R-6714-6)."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.settings import Settings
from scadbuddy.filament.models import SpoolCreate
from scadbuddy.filament.profile_fill import TypeFiller
from scadbuddy.filament.spools import FilamentSpoolStore
from tests.api.test_filament_purchasing import _invoice_body
from tests.api.test_send import API, configure
from tests.filament.test_profile_fill import TREE, local_preset

pytestmark = pytest.mark.requires_postgres

TYPES = "/api/v1/filament/types"
SILK_04 = [{"printer_model": "H2C", "nozzle_diameter": "0.4", "slicer_filament": "GFA05"}]


@pytest.fixture
def settings(settings: Settings) -> Iterator[Settings]:
    """The app reads the test tree as the image's slicer profiles."""
    yield settings.model_copy(update={"slicer_profiles_dir": TREE})


def _material(client: TestClient) -> str:
    rows = client.get("/api/v1/filament/catalog/materials").json()
    found: str = next(row["id"] for row in rows if row["name"] == "PLA")
    return found


def _type(client: TestClient, code: str = "SILK", **over: Any) -> dict[str, Any]:
    body = {
        "code": code,
        "material_id": _material(client),
        "rgba": "#ffffff",
        "label_weight_g": 1000,
    }
    made = client.post(TYPES, json=body | over)
    assert made.status_code == 201, made.text
    row: dict[str, Any] = made.json()
    return row


def test_one_type_is_filled_then_refreshed_then_applied(client: TestClient) -> None:
    made = _type(client, density_g_cm3="1.3")
    # The first presets of a type fill it (R-6714-6): what makeTypeFromSpool does.
    assert client.put(f"{TYPES}/{made['id']}/presets", json=SILK_04).status_code == 200
    row = client.get(f"{TYPES}/{made['id']}").json()
    assert (row["nozzle_temp_min"], row["bed_temp_max"], row["density_g_cm3"]) == (190, 60, "1.300")

    diff = client.post(f"{TYPES}/{made['id']}/fill-from-profile", json={"refresh": True})
    assert diff.status_code == 200, diff.text
    body = diff.json()
    assert body["filled"] == {"density_g_cm3": "1.32"}
    assert body["previous"] == {"density_g_cm3": "1.300"}
    assert body["source"] == {
        "preset_id": "GFA05",
        "name": "Bambu PLA Silk @BBL H2C 0.4 nozzle",
        "origin": "system",
        "printer_model": "H2C",
        "nozzle_diameter": "0.4",
    }
    assert body["written"] is False

    applied = client.post(
        f"{TYPES}/{made['id']}/fill-from-profile", json={"apply": {"density_g_cm3": "1.32"}}
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["written"] is True
    assert client.get(f"{TYPES}/{made['id']}").json()["density_g_cm3"] == "1.320"

    stale = client.post(
        f"{TYPES}/{made['id']}/fill-from-profile", json={"apply": {"nozzle_temp_min": 185}}
    )
    assert stale.status_code == 409, stale.text


def test_a_missing_type_or_a_bad_field_is_refused(client: TestClient) -> None:
    missing = "00000000-0000-4000-8000-000000000001"
    assert client.post(f"{TYPES}/{missing}/fill-from-profile", json={}).status_code == 404
    made = _type(client)
    bad = client.post(f"{TYPES}/{made['id']}/fill-from-profile", json={"apply": {"cost_per_kg": 1}})
    assert bad.status_code == 422, bad.text


def test_a_later_preset_change_does_not_refill(client: TestClient) -> None:
    made = _type(client)
    client.put(f"{TYPES}/{made['id']}/presets", json=SILK_04)
    client.patch(f"{TYPES}/{made['id']}", json={"density_g_cm3": None})
    at_02 = {"printer_model": "H2C", "nozzle_diameter": "0.2", "slicer_filament": "GFA05"}
    again = [*SILK_04, at_02]
    assert client.put(f"{TYPES}/{made['id']}/presets", json=again).status_code == 200
    assert client.get(f"{TYPES}/{made['id']}").json()["density_g_cm3"] is None


@respx.mock
def test_the_bulk_route_fills_every_type_and_reports_each(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/local-presets/2").mock(return_value=httpx.Response(200, json=local_preset()))
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[]))
    local = _type(client, code="LOCAL")
    client.put(
        f"{TYPES}/{local['id']}/presets",
        json=[{"printer_model": "H2C", "nozzle_diameter": "0.4", "slicer_filament": "2"}],
    )
    client.patch(f"{TYPES}/{local['id']}", json={"nozzle_temp_min": None, "density_g_cm3": None})
    bare = _type(client, code="BARE")

    done = client.post(f"{TYPES}/fill-from-profile", json={})
    assert done.status_code == 200, done.text
    found = {one["code"]: one for one in done.json()}
    assert found["LOCAL"]["filled"] == {"nozzle_temp_min": 220, "density_g_cm3": "1.275"}
    assert found["LOCAL"]["source"]["origin"] == "bambuddy_local"
    assert found["LOCAL"]["cost_hint"] == "30"
    assert found["BARE"]["reason"] == "the type has no slicer preset"
    assert client.get(f"{TYPES}/{bare['id']}").json()["density_g_cm3"] is None

    preview = client.post(f"{TYPES}/fill-from-profile", json={"refresh": True}).json()
    assert {one["code"] for one in preview} == {"LOCAL", "BARE"}
    assert all(one["written"] is False for one in preview)


def test_k_values_without_the_shown_ones_are_refused_and_an_empty_list_is_none(
    client: TestClient,
) -> None:
    """R-6714-i: `k_values: true` needs `shown_k_values`; `[]` says the refresh showed none."""
    made = _type(client)
    url = f"{TYPES}/{made['id']}/fill-from-profile"

    omitted = client.post(url, json={"apply": {}, "k_values": True})
    assert omitted.status_code == 422, omitted.text
    assert "shown_k_values" in omitted.text

    # No linked Bambuddy spool, so Bambuddy gives none, as the refresh showed.
    none = client.post(url, json={"apply": {}, "k_values": True, "shown_k_values": []})
    assert none.status_code == 200, none.text
    assert (none.json()["k_values"], none.json()["written"]) == ([], False)


def _linked_k_values(client: TestClient, type_id: str) -> None:
    """A spool of the type linked to Bambuddy spool 40, whose K profile is on Bambuddy
    printer 1, which a ScadBuddy printer links."""
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    pool = state.settings_store.pool
    spool = FilamentSpoolStore(pool).create(SpoolCreate(type_id=UUID(type_id)), actor="ann").id
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id)"
            " VALUES (gen_random_uuid(), 'TEST00000006747', 'H2C', 1)"
        )
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, linked_at)"
            " VALUES (%s, 40, now())",
            (spool,),
        )
    k = {"printer_id": 1, "extruder": 0, "nozzle_diameter": "0.4", "k_value": 0.02}
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=[{"id": 40, "material": "PLA", "k_profiles": [k]}])
    )


@respx.mock
def test_the_route_writes_the_k_values_shown_and_refuses_others(client: TestClient) -> None:
    """R-6714-j: `shown_k_values` through the route: a match writes, a difference is 409,
    and with `k_values: false` they are ignored."""
    configure(client)
    made = _type(client)
    url = f"{TYPES}/{made['id']}/fill-from-profile"
    _linked_k_values(client, made["id"])

    diff = client.post(url, json={"refresh": True})
    assert diff.status_code == 200, diff.text
    shown = [{k: v for k, v in row.items() if k != "type_id"} for row in diff.json()["k_values"]]
    assert [Decimal(row["k_value"]) for row in shown] == [Decimal("0.02")]
    other = [shown[0] | {"k_value": "0.03"}]

    ignored = client.post(url, json={"apply": {}, "k_values": False, "shown_k_values": other})
    assert ignored.status_code == 200, ignored.text
    assert ignored.json()["written"] is False
    assert client.get(f"{TYPES}/{made['id']}").json()["k_values"] == []

    changed = client.post(url, json={"apply": {}, "k_values": True, "shown_k_values": other})
    assert changed.status_code == 409, changed.text
    assert "K values" in changed.json()["detail"]
    assert client.get(f"{TYPES}/{made['id']}").json()["k_values"] == []

    done = client.post(url, json={"apply": {}, "k_values": True, "shown_k_values": shown})
    assert done.status_code == 200, done.text
    assert done.json()["written"] is True
    saved = client.get(f"{TYPES}/{made['id']}").json()["k_values"]
    assert [Decimal(row["k_value"]) for row in saved] == [Decimal("0.02")]


def _spy_fill(monkeypatch: pytest.MonkeyPatch) -> list[list[UUID]]:
    asked: list[list[UUID]] = []
    fill = TypeFiller.fill

    async def spy(self: TypeFiller, settings: Any, type_ids: Any, **kwargs: Any) -> Any:
        asked.append(list(type_ids or []))
        return await fill(self, settings, type_ids, **kwargs)

    monkeypatch.setattr(TypeFiller, "fill", spy)
    return asked


def test_setting_presets_on_an_empty_type_fills_its_fields(client: TestClient) -> None:
    made = _type(client)
    assert made["nozzle_temp_min"] is None
    assert made["density_g_cm3"] is None
    assert client.put(f"{TYPES}/{made['id']}/presets", json=SILK_04).status_code == 200
    row = client.get(f"{TYPES}/{made['id']}").json()
    assert (row["nozzle_temp_min"], row["bed_temp_max"], row["density_g_cm3"]) == (190, 60, "1.320")


def test_a_type_made_or_an_invoice_confirmed_is_not_filled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A type made has no preset, so it is filled when its presets arrive (R-6714-a),
    not by the route that makes it."""
    asked = _spy_fill(monkeypatch)
    supplier = client.post("/api/v1/filament/suppliers", json={"name": "Acme"}).json()["id"]
    kept = _type(client, code="KEPT")
    proposed = {
        "code": "NEW",
        "material_id": _material(client),
        "rgba": "#000000",
        "label_weight_g": 1000,
    }
    lines = [
        {"description": "new", "quantity": 1, "unit_price": "20.00", "proposed_type": proposed},
        {"description": "kept", "quantity": 1, "unit_price": "20.00", "type_id": kept["id"]},
    ]
    invoice = client.post(
        "/api/v1/filament/invoices",
        json=_invoice_body(
            supplier, subtotal="40.00", shipping="0", tax="0", total="40.00", lines=lines
        ),
    )
    assert invoice.status_code == 201, invoice.text
    done = client.post(f"/api/v1/filament/invoices/{invoice.json()['id']}/confirm")
    assert done.status_code == 200, done.text
    assert asked == []


def test_a_fill_that_raises_after_the_presets_are_saved_is_logged_not_raised(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(self: TypeFiller, settings: Any, type_ids: Any, **kwargs: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(TypeFiller, "fill", boom)
    made = _type(client)
    with caplog.at_level("WARNING"):
        put = client.put(f"{TYPES}/{made['id']}/presets", json=SILK_04)
    assert put.status_code == 200, put.text
    assert [row["slicer_filament"] for row in put.json()] == ["GFA05"]
    [warning] = [r for r in caplog.records if r.levelname == "WARNING" and hasattr(r, "error")]
    assert warning.error == "RuntimeError"
