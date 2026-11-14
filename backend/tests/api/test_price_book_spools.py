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
def test_two_alphas_of_one_colour_price_one_record_once(client: TestClient) -> None:
    """#5159 review: FFFFFFFF and FFFFFF80 are one record, priced from the newest spool."""
    configure(client)
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(
            200,
            json=[
                spool(9, rgba="FFFFFF80", cost_per_kg=30),
                spool(3, rgba="FFFFFFFF", cost_per_kg=20),
                spool(5, rgba="FFFFFF", cost_per_kg=40),
                spool(4, material="P" * 101, rgba="000000FF", cost_per_kg=10),
            ],
        )
    )

    rows = client.post(IMPORT).json()["rows"]

    white = next(row for row in rows if row["colour"] == "#FFFFFF")
    assert (white["action"], white["spool_ids"], white["cost_per_kg"]) == (
        "created",
        [3, 5, 9],
        "30",
    )
    long = next(row for row in rows if row["spool_ids"] == [4])
    assert (long["action"], long["reason"]) == ("skipped", "too_long")
    filaments = client.get(BOOK).json()["filaments"]
    assert [(f["colour"], f["cost_per_kg"]) for f in filaments] == [("#FFFFFF", "30")]


@respx.mock
def test_a_quote_never_reads_the_spools(client: TestClient) -> None:
    """Ruling 3: the book is ScadBuddy's; reading it never calls Bambuddy."""
    configure(client)
    inventory = respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[]))
    assert client.get(BOOK).status_code == 200
    assert not inventory.called


# -- density from the spool's slicer preset (#6168) ---------------------------------------


def local_preset(preset_id: int, setting: dict[str, Any], **row: Any) -> dict[str, Any]:
    """A ``GET /local-presets/{id}`` answer: the row with its ``setting`` document."""
    return {
        "id": preset_id,
        "name": f"Preset {preset_id}",
        "preset_type": "filament",
        "source": "orcaslicer",
        "setting": setting,
        **row,
    }


@respx.mock
def test_a_made_record_takes_its_presets_density(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=[spool(4, slicer_filament="2", cost_per_kg=20)])
    )
    preset = respx.get(f"{API}/local-presets/2").mock(
        return_value=httpx.Response(200, json=local_preset(2, {"filament_density": ["1.24"]}))
    )

    (row,) = client.post(IMPORT).json()["rows"]

    assert preset.call_count == 1
    assert (row["action"], row["density_g_cm3"], row["no_density"]) == ("created", "1.24", None)
    (record,) = client.get(BOOK).json()["filaments"]
    assert record["density_g_cm3"] == "1.24"


@respx.mock
def test_an_update_fills_an_empty_density(client: TestClient) -> None:
    configure(client)
    made = add_filament(
        client, brand="Bambu", subtype="Basic", cost_per_kg="20", density_g_cm3=None
    )
    assert made["density_g_cm3"] is None
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=[spool(4, slicer_filament="2", cost_per_kg=20)])
    )
    respx.get(f"{API}/local-presets/2").mock(
        return_value=httpx.Response(200, json=local_preset(2, {"filament_density": "1.275"}))
    )

    (row,) = client.post(IMPORT).json()["rows"]

    # The price did not move, but the density did: the record changed.
    assert (row["action"], row["filament_id"], row["density_g_cm3"]) == (
        "updated",
        made["id"],
        "1.275",
    )
    assert (row["cost_per_kg"], row["previous_cost_per_kg"]) == ("20", "20")
    (record,) = client.get(BOOK).json()["filaments"]
    assert record["density_g_cm3"] == "1.275"
    # Imported again, nothing changes.
    (again,) = client.post(IMPORT).json()["rows"]
    assert (again["action"], again["density_g_cm3"]) == ("unchanged", "1.275")


@respx.mock
def test_an_update_keeps_a_density_someone_entered(client: TestClient) -> None:
    configure(client)
    made = add_filament(
        client, brand="Bambu", subtype="Basic", cost_per_kg="20", density_g_cm3="1.3"
    )
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=[spool(4, slicer_filament="2", cost_per_kg=25)])
    )
    respx.get(f"{API}/local-presets/2").mock(
        return_value=httpx.Response(200, json=local_preset(2, {"filament_density": ["1.24"]}))
    )

    (row,) = client.post(IMPORT).json()["rows"]

    assert (row["action"], row["cost_per_kg"], row["density_g_cm3"], row["no_density"]) == (
        "updated",
        "25",
        "1.3",
        None,
    )
    record = client.get(BOOK).json()["filaments"][0]
    assert (record["id"], record["density_g_cm3"]) == (made["id"], "1.3")


@respx.mock
def test_a_preset_without_a_density_fails_no_import(client: TestClient) -> None:
    """A preset that is gone, cannot be read or gives no density leaves the density
    empty, says so on the row, and imports the price all the same."""
    configure(client)
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(
            200,
            json=[
                spool(1, slicer_filament="11", rgba="FF0000FF", cost_per_kg=20),
                spool(2, slicer_filament="12", rgba="00FF00FF", cost_per_kg=20),
                spool(3, slicer_filament="13", rgba="0000FFFF", cost_per_kg=20),
                spool(4, slicer_filament="14", rgba="FFFF00FF", cost_per_kg=20),
                spool(5, slicer_filament=None, rgba="00FFFFFF", cost_per_kg=20),
            ],
        )
    )
    respx.get(f"{API}/local-presets/11").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{API}/local-presets/12").mock(return_value=httpx.Response(500, text="boom"))
    respx.get(f"{API}/local-presets/13").mock(
        return_value=httpx.Response(200, json=local_preset(13, {"filament_type": ["PLA"]}))
    )
    respx.get(f"{API}/local-presets/14").mock(
        return_value=httpx.Response(200, json=local_preset(14, {"filament_density": ["nope"]}))
    )

    response = client.post(IMPORT)

    assert response.status_code == 200, response.text
    rows = {row["spool_ids"][0]: row for row in response.json()["rows"]}
    assert {i: (r["action"], r["density_g_cm3"], r["no_density"]) for i, r in rows.items()} == {
        1: ("created", None, "not_in_preset"),
        2: ("created", None, "not_in_preset"),
        3: ("created", None, "not_in_preset"),
        4: ("created", None, "not_in_preset"),
        5: ("created", None, "no_preset"),
    }
    assert len(client.get(BOOK).json()["filaments"]) == 5


@respx.mock
def test_one_read_per_preset_from_each_groups_newest_spool(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(
            200,
            json=[
                # One group: only the newest spool's preset is read.
                spool(3, slicer_filament="9", cost_per_kg=20),
                spool(8, slicer_filament="2", cost_per_kg=20),
                # Another group naming the same preset: read once for both.
                spool(4, slicer_filament="2", rgba="000000FF", cost_per_kg=20),
            ],
        )
    )
    older = respx.get(f"{API}/local-presets/9").mock(
        return_value=httpx.Response(200, json=local_preset(9, {"filament_density": ["9.99"]}))
    )
    newest = respx.get(f"{API}/local-presets/2").mock(
        return_value=httpx.Response(200, json=local_preset(2, {"filament_density": ["1.24"]}))
    )

    rows = client.post(IMPORT).json()["rows"]

    assert not older.called
    assert newest.call_count == 1
    assert [row["density_g_cm3"] for row in rows] == ["1.24", "1.24"]
