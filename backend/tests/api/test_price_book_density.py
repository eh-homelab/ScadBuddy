"""A price-book filament's density from the slicer's profiles (#6136): the spool import
and "Fill densities" write it where a record has none, the book shows where each came
from or what it falls back to, and the estimate weighs a record with none at its
profile's density, refusing only when no source gives one."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from tests.api.test_order_estimate import ESTIMATE, box, output_of
from tests.api.test_price_book import BOOK, add_filament
from tests.api.test_price_book_spools import IMPORT, spool
from tests.api.test_send import API, configure
from tests.orders.test_density import PROFILES

FILL = f"{BOOK}/fill-densities"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"slicer_profiles_dir": PROFILES})


def basic(client: TestClient, **fields: Any) -> dict[str, Any]:
    return add_filament(client, brand="Bambu Lab", subtype="Basic", density_g_cm3=None, **fields)


def test_the_book_shows_each_density_or_its_fallback(client: TestClient) -> None:
    empty = basic(client)
    own = add_filament(client, colour="#000000", density_g_cm3="1.3")
    none = add_filament(
        client, colour="#00FF00", subtype="Galaxy", brand="Bambu Lab", density_g_cm3=None
    )
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    assert book[empty["id"]]["fallback_density"] == {
        "density_g_cm3": "1.26",
        "profile": "Bambu PLA Basic @base",
    }
    assert book[own["id"]]["density_profile"] is None
    assert book[own["id"]]["fallback_density"] is None
    # No source: nothing to fall back to, which the page says.
    assert book[none["id"]]["fallback_density"] is None


def test_fill_densities_writes_the_empty_ones_only(client: TestClient) -> None:
    empty = basic(client)
    own = add_filament(client, colour="#000000", density_g_cm3="1.3")
    none = add_filament(
        client, colour="#00FF00", subtype="Galaxy", brand="Bambu Lab", density_g_cm3=None
    )

    response = client.post(FILL)

    assert response.status_code == 200, response.text
    rows = {row["filament_id"]: row for row in response.json()["rows"]}
    assert set(rows) == {empty["id"], none["id"]}
    assert (rows[empty["id"]]["density_g_cm3"], rows[empty["id"]]["density_profile"]) == (
        "1.26",
        "Bambu PLA Basic @base",
    )
    assert rows[none["id"]]["density_g_cm3"] is None
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    filled = book[empty["id"]]
    assert (filled["density_g_cm3"], filled["density_profile"]) == ("1.26", "Bambu PLA Basic @base")
    assert book[own["id"]]["density_g_cm3"] == "1.3"

    # The editor's save sends the same density back: its preset stays.
    kept = client.patch(
        f"{BOOK}/filaments/{empty['id']}", json={"density_g_cm3": "1.260", "cost_per_kg": "9"}
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["density_profile"] == "Bambu PLA Basic @base"
    # A person's density replaces the profile's, and names no profile.
    edited = client.patch(f"{BOOK}/filaments/{empty['id']}", json={"density_g_cm3": "1.25"})
    assert edited.status_code == 200, edited.text
    assert (edited.json()["density_g_cm3"], edited.json()["density_profile"]) == ("1.25", None)
    assert [row["filament_id"] for row in client.post(FILL).json()["rows"]] == [none["id"]]


@respx.mock
def test_the_spool_import_fills_an_empty_density(client: TestClient) -> None:
    configure(client)
    own = add_filament(client, brand="Bambu Lab", subtype="Basic", density_g_cm3="1.3")
    empty = add_filament(
        client, brand="Bambu Lab", subtype="Galaxy", colour="#00FF00", density_g_cm3=None
    )
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(
            200,
            json=[
                # Matches `own`: its density stays.
                spool(1, brand="Bambu Lab", cost_per_kg=20, slicer_filament="GFA00"),
                # Matches `empty`: its preset id (Bambu PLA Silk) gives the density the
                # name "Galaxy" cannot.
                spool(
                    2,
                    brand="Bambu Lab",
                    subtype="Galaxy",
                    rgba="00FF00FF",
                    cost_per_kg=26,
                    slicer_filament="GFA05",
                ),
                # A new record, from its names.
                spool(3, brand="Bambu Lab", rgba="0000FFFF", cost_per_kg=20),
                # A new record no source covers.
                spool(4, brand="Cookiecad", subtype="Magic", rgba="FF00FFFF", cost_per_kg=30),
            ],
        )
    )

    rows = {row["spool_ids"][0]: row for row in client.post(IMPORT).json()["rows"]}

    assert (rows[1]["density_g_cm3"], rows[1]["density_profile"]) == ("1.3", None)
    assert rows[2]["action"] == "updated"
    assert (rows[2]["density_g_cm3"], rows[2]["density_profile"]) == (
        "1.32",
        "Bambu PLA Silk @base",
    )
    assert (rows[3]["action"], rows[3]["density_g_cm3"]) == ("created", "1.26")
    assert (rows[4]["action"], rows[4]["density_g_cm3"]) == ("created", None)
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    assert book[own["id"]]["density_g_cm3"] == "1.3"
    assert (book[empty["id"]]["density_g_cm3"], book[empty["id"]]["density_profile"]) == (
        "1.32",
        "Bambu PLA Silk @base",
    )
    again = {row["spool_ids"][0]: row for row in client.post(IMPORT).json()["rows"]}
    assert again[2]["action"] == "unchanged"


def test_the_estimate_falls_back_to_the_profile(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = output_of(client, model, paths, [box(20, 20, 20)])
    empty = basic(client)
    response = client.get(ESTIMATE, params={"output_id": output_id, "filament_id": empty["id"]})
    assert response.status_code == 200, response.text
    body = response.json()
    # 8 cm3 x 1.26 x 1.15 = 11.592
    assert body["grams"] == "11.59"
    assert body["densities"] == [
        {
            "filament_id": empty["id"],
            "density_g_cm3": "1.26",
            "source": "profile",
            "profile": "Bambu PLA Basic @base",
        }
    ]
    none = add_filament(
        client, colour="#00FF00", subtype="Galaxy", brand="Bambu Lab", density_g_cm3=None
    )
    refused = client.get(ESTIMATE, params={"output_id": output_id, "filament_id": none["id"]})
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"].endswith("/filament_density")
    assert "no slicer profile" in refused.json()["detail"]
