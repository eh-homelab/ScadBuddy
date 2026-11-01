"""`POST /orders/price-book/import-spools` (#4253, spec 2026-10-18 §8.3, Ruling 3): the
price book's filaments imported from Bambuddy's spools, on click, through the orders
Bambuddy seam (§8), which lights up when a URL is set, without a restart."""

from __future__ import annotations

from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient

from tests.api.test_price_book import BOOK, add_filament
from tests.api.test_send import API, configure

IMPORT = f"{BOOK}/import-spools"


def spool(spool_id: int, **fields: Any) -> dict[str, Any]:
    return {
        "id": spool_id,
        "material": "PLA",
        "subtype": "Basic",
        "brand": "Bambu",
        "color_name": "Jade White",
        "rgba": "FFFFFFFF",
        "label_weight": 1000,
        "weight_used": 0.0,
        "cost_per_kg": None,
        "archived_at": None,
        **fields,
    }


def test_without_bambuddy_there_is_nothing_to_import(client: TestClient) -> None:
    assert client.get(BOOK).json()["spool_import"] is False
    response = client.post(IMPORT)
    assert response.status_code == 409
    assert response.json()["type"] == "https://scadbuddy.dev/problems/orders/not_linked"


@respx.mock
def test_spools_make_and_update_the_records(client: TestClient) -> None:
    configure(client)
    # Set without a restart: the seam follows the running settings.
    assert client.get(BOOK).json()["spool_import"] is True
    priced = add_filament(client, brand="BAMBU", subtype="basic", cost_per_kg="10")
    inventory = respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(
            200,
            json=[
                # Matched to `priced` by identity, in any case; the newest spool's price.
                spool(3, cost_per_kg=24.0),
                spool(8, cost_per_kg=26.5),
                spool(5, cost_per_kg=None),
                # A new filament.
                spool(4, material="PETG", color_name="Green", rgba="00ae42ff", cost_per_kg=20),
                # Only a colour's name: no record can carry it.
                spool(6, color_name="Grey", rgba=None, cost_per_kg=18),
                # No price anywhere.
                spool(7, rgba="000000FF", color_name="Black"),
            ],
        )
    )

    response = client.post(IMPORT)

    assert response.status_code == 200, response.text
    assert inventory.calls.last.request.url.params["include_archived"] == "false"
    rows = {tuple(row["spool_ids"]): row for row in response.json()["rows"]}
    white = rows[(3, 5, 8)]
    assert white["action"] == "updated"
    assert white["filament_id"] == priced["id"]
    assert (white["cost_per_kg"], white["previous_cost_per_kg"]) == ("26.5", "10")
    assert (white["min_cost_per_kg"], white["max_cost_per_kg"]) == ("24", "26.5")
    petg = rows[(4,)]
    assert petg["action"] == "created"
    assert (petg["colour"], petg["colour_name"], petg["cost_per_kg"]) == ("#00AE42", "Green", "20")
    assert petg["min_cost_per_kg"] is None
    assert (rows[(6,)]["action"], rows[(6,)]["reason"]) == ("skipped", "no_colour")
    assert (rows[(7,)]["action"], rows[(7,)]["reason"]) == ("skipped", "no_price")

    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    assert book[priced["id"]]["cost_per_kg"] == "26.5"
    made = book[petg["filament_id"]]
    assert (made["material"], made["subtype"], made["brand"], made["colour"]) == (
        "PETG",
        "Basic",
        "Bambu",
        "#00AE42",
    )
    assert len(book) == 2

    again = client.post(IMPORT).json()["rows"]
    assert {row["action"] for row in again if row["reason"] is None} == {"unchanged"}


@respx.mock
def test_a_quote_never_reads_the_spools(client: TestClient) -> None:
    """Ruling 3: the book is ScadBuddy's; reading it never calls Bambuddy."""
    configure(client)
    inventory = respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[]))
    assert client.get(BOOK).status_code == 200
    assert not inventory.called
