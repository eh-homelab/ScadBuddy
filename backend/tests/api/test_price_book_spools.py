"""`POST /orders/price-book/import-spools` (#4253, spec 2026-10-18 §8.3, Ruling 3): the
price book's filaments imported from Bambuddy's spools, on click, through the orders
Bambuddy seam (§8), which lights up when a URL is set, without a restart."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import psycopg
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


def no_density_sources() -> None:
    """Bambuddy's filament catalogue and AMS assignments, which the import reads for a
    density (#6136), empty."""
    respx.get(f"{API}/filament-catalog/").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/inventory/assignments").mock(return_value=httpx.Response(200, json=[]))


def test_without_bambuddy_there_is_nothing_to_import(client: TestClient) -> None:
    assert client.get(BOOK).json()["spool_import"] is False
    response = client.post(IMPORT)
    assert response.status_code == 409
    assert response.json()["type"] == "https://scadbuddy.dev/problems/orders/not_linked"


@respx.mock
def test_spools_make_and_update_the_records(client: TestClient) -> None:
    configure(client)
    no_density_sources()
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
    no_density_sources()
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


def _inventory(*spools: dict[str, Any]) -> None:
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=list(spools)))


CLEAR = {"subtype": "Translucent", "color_name": "Clear", "rgba": "00000000"}


@respx.mock
def test_a_clear_spool_keeps_its_alpha_stops_and_effect(client: TestClient) -> None:
    """#6209: a clear spool is ``#000000`` with alpha 0, not black; the stops and the
    effect come too, and the record's colour is the import's."""
    configure(client)
    no_density_sources()
    _inventory(
        spool(3, **CLEAR, effect_type="translucent", cost_per_kg=20),
        spool(4, color_name="Aurora", extra_colors="7f3696,3a8fd2ff", cost_per_kg=26),
    )

    rows = {tuple(r["spool_ids"]): r for r in client.post(IMPORT).json()["rows"]}

    clear = rows[(3,)]
    assert (clear["action"], clear["colour"], clear["alpha"], clear["effect"]) == (
        "created",
        "#000000",
        0,
        "translucent",
    )
    assert clear["colour_source"] == "import"
    assert rows[(4,)]["extra_colours"] == ["#7F3696", "#3A8FD2FF"]
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    made = book[clear["filament_id"]]
    assert (made["colour"], made["alpha"], made["effect"], made["colour_source"]) == (
        "#000000",
        0,
        "translucent",
        "import",
    )
    assert book[rows[(4,)]["filament_id"]]["extra_colours"] == ["#7F3696", "#3A8FD2FF"]


@respx.mock
def test_a_persons_colour_is_kept_and_never_duplicated(client: TestClient) -> None:
    """#6209: a colour a person edits is ``manual``; the next import matches the record by
    its spools (then by the spools' RGB), keeps that colour, and makes no second record."""
    configure(client)
    no_density_sources()
    _inventory(spool(3, **CLEAR, cost_per_kg=20))
    made = client.post(IMPORT).json()["rows"][0]
    url = f"{BOOK}/filaments/{made['filament_id']}"
    edited = client.patch(url, json={"colour": "#EEF3F4", "alpha": 255}).json()
    assert edited["colour_source"] == "manual"

    # The same spool, a new price: matched by the spool link.
    _inventory(spool(3, **CLEAR, effect_type="translucent", cost_per_kg=22))
    again = client.post(IMPORT).json()["rows"][0]
    assert (again["action"], again["filament_id"]) == ("updated", made["filament_id"])
    assert (again["colour"], again["alpha"], again["colour_source"]) == ("#EEF3F4", 255, "manual")

    # A new spool of it, the old one gone: matched by the spools' RGB.
    _inventory(spool(9, **CLEAR, cost_per_kg=23))
    newer = client.post(IMPORT).json()["rows"][0]
    assert (newer["action"], newer["filament_id"]) == ("updated", made["filament_id"])

    book = client.get(BOOK).json()["filaments"]
    assert [(f["id"], f["colour"], f["alpha"], f["cost_per_kg"]) for f in book] == [
        (made["filament_id"], "#EEF3F4", 255, "23")
    ]

    # Another filament whose RGB is the colour the person set: skipped, not a duplicate.
    other = {**CLEAR, "rgba": "EEF3F4FF"}
    _inventory(spool(9, **CLEAR, cost_per_kg=23), spool(10, **other, cost_per_kg=5))
    rows = {tuple(r["spool_ids"]): r for r in client.post(IMPORT).json()["rows"]}
    assert (rows[(10,)]["action"], rows[(10,)]["reason"]) == ("skipped", "colour_taken")
    assert len(client.get(BOOK).json()["filaments"]) == 1


@respx.mock
def test_the_import_fills_the_look_of_an_imported_record(client: TestClient) -> None:
    """#6209: the backfill. A record whose colour is the import's takes the spool's alpha,
    stops and effect on the next import; handing a colour back (``colour_source``
    ``import``) lets it."""
    configure(client)
    no_density_sources()
    old = add_filament(client, subtype="Translucent", colour_name="Clear", colour="#000000")
    client.patch(f"{BOOK}/filaments/{old['id']}", json={"colour_source": "import"})
    _inventory(spool(3, **CLEAR, effect_type="translucent", cost_per_kg=old["cost_per_kg"]))

    row = client.post(IMPORT).json()["rows"][0]

    assert (row["action"], row["filament_id"], row["alpha"], row["effect"]) == (
        "updated",
        old["id"],
        0,
        "translucent",
    )
    assert client.post(IMPORT).json()["rows"][0]["action"] == "unchanged"


@respx.mock
def test_a_spool_with_a_corrected_material_is_another_filaments(client: TestClient) -> None:
    """#6211 review: the spool link matches only a record of the group's material, subtype
    and brand, so a spool whose material was corrected never takes over its old record."""
    configure(client)
    no_density_sources()
    red = {"rgba": "FF0000FF", "color_name": "Red"}
    _inventory(spool(1, **red, cost_per_kg=20), spool(2, **red, cost_per_kg=21))
    made = client.post(IMPORT).json()["rows"][0]
    assert (made["action"], made["spool_ids"]) == ("created", [1, 2])

    _inventory(spool(1, **red, cost_per_kg=20), spool(2, **red, material="PETG", cost_per_kg=30))
    rows = {tuple(r["spool_ids"]): r for r in client.post(IMPORT).json()["rows"]}

    assert (rows[(1,)]["action"], rows[(1,)]["filament_id"]) == ("updated", made["filament_id"])
    assert rows[(1,)]["cost_per_kg"] == "20"
    assert rows[(2,)]["action"] == "created"
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    old = book[made["filament_id"]]
    assert (old["material"], old["cost_per_kg"]) == ("PLA", "20")
    assert book[rows[(2,)]["filament_id"]]["material"] == "PETG"


@respx.mock
def test_a_split_group_never_takes_over_its_record(client: TestClient) -> None:
    """#6211 review: one spool's colour corrected in Bambuddy splits a record's spools. The
    record stays with the spools of its RGB, though the moved spool's group sorts first."""
    configure(client)
    no_density_sources()
    red = {"rgba": "FF0000FF", "color_name": "Red"}
    _inventory(*(spool(i, **red, cost_per_kg=20) for i in (1, 2, 3)))
    made = client.post(IMPORT).json()["rows"][0]

    blue = {"rgba": "0000FFFF", "color_name": "Blue"}
    _inventory(
        spool(1, **red, cost_per_kg=20),
        spool(2, **red, cost_per_kg=20),
        spool(3, **blue, cost_per_kg=25),
    )
    rows = client.post(IMPORT).json()["rows"]

    assert [r["colour"] for r in rows] == ["#0000FF", "#FF0000"]
    blue_row, red_row = rows
    assert (blue_row["action"], red_row["filament_id"]) == ("created", made["filament_id"])
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    assert book[made["filament_id"]]["colour"] == "#FF0000"
    assert book[blue_row["filament_id"]]["colour"] == "#0000FF"


MIGRATION = (
    Path(__file__).parents[2] / "scadbuddy/migrations/20261114T1607Z_filaments_alpha_effect.sql"
)


@respx.mock
def test_a_record_from_before_the_link_follows_a_colour_fix(
    client: TestClient, pg_conninfo: str
) -> None:
    """#6211 review: the migration's backfill, run on a record a person added by hand,
    links it to no spools, so a colour fixed later is the one its spools are found by,
    never the typo's."""
    configure(client)
    no_density_sources()
    made = add_filament(client, colour="#0000EE", colour_name="Blue", cost_per_kg="20")
    url = f"{BOOK}/filaments/{made['id']}"
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        # The row as the new columns' defaults left it, then the migration's updates.
        conn.execute(
            "UPDATE filaments SET colour_source = 'import', spool_colour = NULL WHERE id = %s",
            (made["id"],),
        )
        sql = "\n".join(
            line
            for line in MIGRATION.read_text(encoding="utf-8").splitlines()
            if not line.startswith("--")
        )
        for statement in sql.split(";"):
            if statement.strip().startswith("UPDATE"):
                conn.execute(statement.encode())
    assert client.patch(url, json={"colour": "#0000FF"}).json()["colour_source"] == "manual"
    blue = {"rgba": "0000FFFF", "color_name": "Blue"}
    typo = {"rgba": "0000EEFF", "color_name": "Blue"}
    _inventory(spool(1, **blue, cost_per_kg=25), spool(2, **typo, cost_per_kg=30))

    rows = {tuple(r["spool_ids"]): r for r in client.post(IMPORT).json()["rows"]}

    assert (rows[(1,)]["action"], rows[(1,)]["filament_id"]) == ("updated", made["id"])
    assert rows[(2,)]["action"] == "created"
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    assert (book[made["id"]]["colour"], book[made["id"]]["cost_per_kg"]) == ("#0000FF", "25")


@respx.mock
def test_a_spool_link_wins_over_a_colour_in_any_group_order(client: TestClient) -> None:
    """#6211 review: a record's one spool recoloured, and a new spool of its old colour.
    The record follows its spool (the link), whichever group sorts first; the new spool
    is a new record."""
    configure(client)
    no_density_sources()
    blue = {"rgba": "0000FFFF", "color_name": "Blue"}
    red = {"rgba": "FF0000FF", "color_name": "Red"}
    _inventory(spool(1, **blue, cost_per_kg=20))
    made = client.post(IMPORT).json()["rows"][0]

    _inventory(spool(1, **red, cost_per_kg=20), spool(2, **blue, cost_per_kg=25))
    rows = client.post(IMPORT).json()["rows"]

    assert [r["colour"] for r in rows] == ["#0000FF", "#FF0000"]
    blue_row, red_row = rows
    assert (red_row["action"], red_row["filament_id"]) == ("updated", made["filament_id"])
    assert (blue_row["action"], blue_row["spool_ids"]) == ("created", [2])
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    assert book[made["filament_id"]]["colour"] == "#FF0000"
    assert book[blue_row["filament_id"]]["colour"] == "#0000FF"
