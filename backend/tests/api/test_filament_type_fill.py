"""`POST /filament/types/{id}/fill-from-profile` and `POST /filament/types/fill-from-profile`
(#6714), and the fill after a type is made (R-6714-6)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.filament.profile_fill import TypeFiller
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
