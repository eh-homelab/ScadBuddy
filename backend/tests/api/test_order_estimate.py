"""`GET /orders/estimate` (#4250, spec 2026-10-18 §5.3, plan Ruling 9): a rough estimate
from a render's part volumes, every plate of it, never a quote."""

from __future__ import annotations

from typing import Any

import trimesh
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.render.bambu3mf import PlateParts, write_plates_3mf
from scadbuddy.render.split import ColourPart
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_outputs import _finished_job
from tests.support.operations import press

ESTIMATE = "/api/v1/orders/estimate"
BOOK = "/api/v1/orders/price-book"


def output_of(client: TestClient, model: str, paths: DataPaths, *plates: list[Any]) -> str:
    """An output whose 3MF lays ``plates`` out, each a list of meshes."""
    created: dict[str, Any] = client.post(
        f"/api/v1/models/{model}/outputs",
        json={"job_id": _finished_job(client, model)},
        headers=press(),
    ).json()
    write_plates_3mf(
        [
            PlateParts(
                tuple(
                    ColourPart(1, f"Color {n}", "#FF0000", mesh)
                    for n, mesh in enumerate(meshes, start=1)
                ),
                (1,),
            )
            for meshes in plates
        ],
        ["#FF0000"],
        paths.output_dir(model, created["id"]) / "model.3mf",
        thumbnails=None,
    )
    output_id: str = created["id"]
    return output_id


def filament(client: TestClient, **fields: Any) -> str:
    made = client.post(
        f"{BOOK}/filaments",
        json={"material": "PLA", "colour": "#FF0000", "cost_per_kg": "25.5", **fields},
    )
    assert made.status_code == 201, made.text
    filament_id: str = made.json()["id"]
    return filament_id


def box(*extents: float) -> trimesh.Trimesh:
    made: trimesh.Trimesh = trimesh.creation.box(extents=extents)
    return made


def test_the_estimate_sums_every_plate(client: TestClient, model: str, paths: DataPaths) -> None:
    """20 x 20 x 20 on plate 1 and 30 x 30 x 10 on plate 2: 17 cm3 x 1.24 g/cm3 x 1.15."""
    output_id = output_of(client, model, paths, [box(20, 20, 20)], [box(30, 30, 10)])
    filament_id = filament(client, density_g_cm3="1.24")
    response = client.get(ESTIMATE, params={"output_id": output_id, "filament_id": filament_id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["estimate"] is True
    assert (body["grams"], body["factor"], body["currency"], body["minor_exponent"]) == (
        "24.24",
        "1.15",
        "USD",
        2,
    )
    # 24.24 g x 25.5 / 1000 = 0.61812
    assert body["filament_minor"] == 62
    assert [(part["plate"], round(part["volume_mm3"])) for part in body["parts"]] == [
        (1, 8000),
        (2, 9000),
    ]
    assert body["unknown_parts"] == 0


def test_a_part_with_no_volume_is_listed_as_unknown(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    open_box = box(10, 10, 10)
    open_box.update_faces([index != 0 for index in range(len(open_box.faces))])
    output_id = output_of(client, model, paths, [box(20, 20, 20)], [open_box])
    filament_id = filament(client, density_g_cm3="1.24")
    body = client.get(ESTIMATE, params={"output_id": output_id, "filament_id": filament_id}).json()
    assert body["unknown_parts"] == 1
    unknown = [part for part in body["parts"] if part["volume_mm3"] is None]
    assert [part["plate"] for part in unknown] == [2]
    # 8 cm3 x 1.24 x 1.15 = 11.408
    assert body["grams"] == "11.41"


def test_a_filament_without_density_gives_none(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = output_of(client, model, paths, [box(20, 20, 20)])
    plain = filament(client)
    refused = client.get(ESTIMATE, params={"output_id": output_id, "filament_id": plain})
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"].endswith("/filament_density")
    dense = filament(client, colour="#000000", density_g_cm3="1.24")
    unknown_filament = client.get(ESTIMATE, params={"output_id": output_id, "filament_id": "x"})
    assert unknown_filament.status_code == 404
    unknown_output = client.get(ESTIMATE, params={"output_id": "0" * 32, "filament_id": dense})
    assert unknown_output.status_code == 404
    assert client.get(ESTIMATE, params={"output_id": output_id}).status_code == 422


def test_the_estimate_records_nothing(client: TestClient, model: str, paths: DataPaths) -> None:
    """Review Focus 8: a read, never a quote."""
    customer = create_customer(client)
    order = create_order(client, customer["id"])
    output_id = output_of(client, model, paths, [box(20, 20, 20)])
    filament_id = filament(client, density_g_cm3="1.24")
    events = client.get(f"/api/v1/orders/{order['id']}/events").json()
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert (
        client.get(
            ESTIMATE, params={"output_id": output_id, "filament_id": filament_id}
        ).status_code
        == 200
    )
    assert client.get(f"/api/v1/orders/{order['id']}/events").json() == events
    assert client.get(f"/api/v1/orders/{order['id']}").json() == detail
