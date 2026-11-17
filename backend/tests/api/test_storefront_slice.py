"""The shop slice profile's choices (#6153, #6574): the options route offers the price
book's materials and the filament types whose presets a slot of a material is sliced
with."""

from __future__ import annotations

import respx
from fastapi.testclient import TestClient

from tests.support.sb_rows import seed_app_type

OPTIONS = "/api/v1/storefront/slice-profile/options"


@respx.mock
def test_the_options_list_the_filament_types(client: TestClient) -> None:
    """#6574: the types not archived, by code, each with its material; nothing is read
    of Bambuddy (respx is active with no route, so a call would fail)."""
    client.put("/api/v1/settings", json={"bambuddy_url": "https://bambuddy.test"})
    pla = seed_app_type(client, "PLA-W", "PLA", {"0.4": "Bambu PLA Basic @BBL H2C"})
    petg = seed_app_type(client, "PETG-K", "PETG")
    answer = client.get(OPTIONS)
    assert answer.status_code == 200, answer.text
    types = answer.json()["types"]
    assert [(t["id"], t["code"], t["material"]) for t in types] == [
        (str(petg), "PETG-K", "PETG"),
        (str(pla), "PLA-W", "PLA"),
    ]
    assert types[1]["label"] == "PLA-W PLA"


def test_the_options_list_the_price_books_materials(client: TestClient) -> None:
    answer = client.get(OPTIONS)
    assert answer.status_code == 200, answer.text
    materials = answer.json()["materials"]
    names = [m["material"] for m in materials]
    assert names == sorted(names, key=str.casefold)
