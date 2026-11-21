"""`POST /orders/price-book/import-spools` (#4253, spec 2026-10-18 §8.3, Ruling 3): the
price book's filaments imported from ScadBuddy's own spools (#6262 Task 9), on click,
with or without a Bambuddy. Each test writes the SB spools as a Bambuddy pull would
(`_inventory`), numbered by the ids it names plus `OFFSET`."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.filament.bambuddy_pull import pull
from scadbuddy.filament.component import FILAMENT, Filament
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.spools import fill_plan, shelf_spools
from tests.api.test_price_book import BOOK, add_filament
from tests.api.test_send import API, configure
from tests.support.sb_rows import seed_sb_spools

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


#: An SB spool's number is its Bambuddy id plus this, so a reader that took one for the
#: other fails; `_ids` reads a row's spool numbers back as the Bambuddy ids the test names.
OFFSET = 100


def _ids(row: dict[str, Any]) -> tuple[int, ...]:
    return tuple(number - OFFSET for number in row["spool_ids"])


#: Each `_inventory` writes as a later Bambuddy change than the last.
_CLOCK = itertools.count(1)


def _inventory(client: TestClient, *spools: dict[str, Any]) -> None:
    """ScadBuddy's spools become ``spools`` (Bambuddy's shape), as a pull of them makes
    them: SB spool #``id`` for each, made or changed, and every other one archived."""
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    filament: Filament = state.components.get(FILAMENT)
    stamp = datetime(2026, 11, 15, tzinfo=UTC) + timedelta(minutes=next(_CLOCK))
    for row in spools:
        bb = BbSpool.model_validate(row | {"updated_at": stamp.isoformat()})
        if filament.inventory.bb_to_sb([bb.id]):
            assert not pull(filament, [bb], now=stamp).failed_bb_ids, bb.id
        else:
            seed_sb_spools(filament, [bb], offset=OFFSET)
    listed = {row["id"] + OFFSET for row in spools}
    for kept in filament.spools.list():
        if kept.number not in listed:
            filament.spools.archive(kept.id, True, actor="test")


def test_the_import_needs_no_bambuddy(client: TestClient) -> None:
    """#6262: the import reads ScadBuddy's spools, so it runs with no Bambuddy URL."""
    assert client.get(BOOK).json()["spool_import"] is True
    _inventory(client, spool(3, cost_per_kg=24.0))

    response = client.post(IMPORT)

    assert response.status_code == 200, response.text
    [row] = response.json()["rows"]
    assert (row["action"], list(_ids(row)), row["cost_per_kg"]) == ("created", [3], "24")


def test_spools_make_and_update_the_records(client: TestClient) -> None:
    priced = add_filament(client, brand="BAMBU", subtype="basic", cost_per_kg="10")
    _inventory(
        client,
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
    )

    response = client.post(IMPORT)

    assert response.status_code == 200, response.text
    rows = {_ids(row): row for row in response.json()["rows"]}
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


def test_two_alphas_of_one_colour_are_two_records(client: TestClient) -> None:
    """#6316: FFFFFFFF and FFFFFF (one record, priced from the newest) and FFFFFF80 (a
    record of its own, beside it) are two filaments, as the identity index holds alpha."""
    _inventory(
        client,
        spool(9, rgba="FFFFFF80", cost_per_kg=30),
        spool(3, rgba="FFFFFFFF", cost_per_kg=20),
        spool(5, rgba="FFFFFF", cost_per_kg=40),
    )

    rows = {_ids(row): row for row in client.post(IMPORT).json()["rows"]}

    assert (rows[(3, 5)]["action"], rows[(3, 5)]["alpha"], rows[(3, 5)]["cost_per_kg"]) == (
        "created",
        255,
        "40",
    )
    assert (rows[(9,)]["action"], rows[(9,)]["alpha"], rows[(9,)]["cost_per_kg"]) == (
        "created",
        0x80,
        "30",
    )
    filaments = client.get(BOOK).json()["filaments"]
    assert sorted((f["colour"], f["alpha"], f["cost_per_kg"]) for f in filaments) == [
        ("#FFFFFF", 0x80, "30"),
        ("#FFFFFF", 255, "40"),
    ]


@respx.mock
def test_a_quote_never_reads_the_spools(client: TestClient) -> None:
    """Ruling 3: the book is ScadBuddy's; reading it never calls Bambuddy."""
    configure(client)
    inventory = respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[]))
    assert client.get(BOOK).status_code == 200
    assert not inventory.called


CLEAR = {"subtype": "Translucent", "color_name": "Clear", "rgba": "00000000"}


def test_a_clear_spool_keeps_its_alpha_stops_and_effect(client: TestClient) -> None:
    """#6209: a clear spool is ``#000000`` with alpha 0, not black; the stops and the
    effect come too, and the record's colour is the import's."""
    _inventory(
        client,
        spool(3, **CLEAR, effect_type="translucent", cost_per_kg=20),
        spool(4, color_name="Aurora", extra_colors="7f3696,3a8fd2ff", cost_per_kg=26),
    )

    rows = {_ids(r): r for r in client.post(IMPORT).json()["rows"]}

    clear = rows[(3,)]
    assert (clear["action"], clear["colour"], clear["alpha"], clear["effect"]) == (
        "created",
        "#000000",
        0,
        "translucent",
    )
    assert clear["colour_source"] == "import"
    # ScadBuddy keeps each stop as `#RRGGBBAA` (#6262).
    assert rows[(4,)]["extra_colours"] == ["#7F3696FF", "#3A8FD2FF"]
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    made = book[clear["filament_id"]]
    assert (made["colour"], made["alpha"], made["effect"], made["colour_source"]) == (
        "#000000",
        0,
        "translucent",
        "import",
    )
    assert book[rows[(4,)]["filament_id"]]["extra_colours"] == ["#7F3696FF", "#3A8FD2FF"]


def test_a_persons_colour_is_kept_and_never_duplicated(client: TestClient) -> None:
    """#6209: a colour a person edits is ``manual``; the next import matches the record by
    its spools (then by the spools' RGB), keeps that colour, and makes no second record."""
    _inventory(client, spool(3, **CLEAR, cost_per_kg=20))
    made = client.post(IMPORT).json()["rows"][0]
    url = f"{BOOK}/filaments/{made['filament_id']}"
    # The person's colour keeps the filament's alpha: a clear filament stays one (#6316).
    edited = client.patch(url, json={"colour": "#EEF3F4"}).json()
    assert edited["colour_source"] == "manual"

    # The same spool, a new price: matched by the spool link.
    _inventory(client, spool(3, **CLEAR, effect_type="translucent", cost_per_kg=22))
    again = client.post(IMPORT).json()["rows"][0]
    assert (again["action"], again["filament_id"]) == ("updated", made["filament_id"])
    assert (again["colour"], again["alpha"], again["colour_source"]) == ("#EEF3F4", 0, "manual")

    # A new spool of it, the old one gone: matched by the spools' RGB.
    _inventory(client, spool(9, **CLEAR, cost_per_kg=23))
    newer = client.post(IMPORT).json()["rows"][0]
    assert (newer["action"], newer["filament_id"]) == ("updated", made["filament_id"])

    book = client.get(BOOK).json()["filaments"]
    assert [(f["id"], f["colour"], f["alpha"], f["cost_per_kg"]) for f in book] == [
        (made["filament_id"], "#EEF3F4", 0, "23")
    ]
    # Linked to its spools, so "Use spool colour" is offered; a record added by hand
    # that no import has linked is not.
    assert book[0]["spool_linked"] is True
    by_hand = client.post(
        f"{BOOK}/filaments",
        json={"material": "ABS", "colour": "#123456", "cost_per_kg": "20"},
    ).json()
    assert (by_hand["colour_source"], by_hand["spool_linked"]) == ("manual", False)
    client.post(f"{BOOK}/filaments/{by_hand['id']}/archive")

    # Another filament whose colour is the one the person set: skipped, not a duplicate.
    other = {**CLEAR, "rgba": "EEF3F400"}
    _inventory(client, spool(9, **CLEAR, cost_per_kg=23), spool(10, **other, cost_per_kg=5))
    rows = {_ids(r): r for r in client.post(IMPORT).json()["rows"]}
    assert (rows[(10,)]["action"], rows[(10,)]["reason"]) == ("skipped", "colour_taken")
    assert len(client.get(BOOK).json()["filaments"]) == 1


def test_an_opaque_stand_in_keeps_its_clear_spools_by_their_link(client: TestClient) -> None:
    """Task 9 review ruling: prod's two clear stand-ins (`20261114T1607Z`) are opaque
    records (alpha 255, a colour a person set) linked to clear spools (``#00000000``). A
    link by id matches its spools whatever their alpha: the import updates the record and
    makes no second one, and an item print takes its linked clear spool."""
    _inventory(client, spool(3, **CLEAR, cost_per_kg=20))
    made = client.post(IMPORT).json()["rows"][0]
    url = f"{BOOK}/filaments/{made['filament_id']}"
    edited = client.patch(url, json={"colour": "#EEF3F4", "alpha": 255}).json()
    assert (edited["colour_source"], edited["alpha"]) == ("manual", 255)

    # The same spool, a new price, and a second spool of it: matched by the link.
    _inventory(
        client,
        spool(3, **CLEAR, weight_used=600.0, cost_per_kg=22),
        spool(9, **CLEAR, cost_per_kg=23),
    )
    [again] = client.post(IMPORT).json()["rows"]
    assert (again["action"], again["filament_id"]) == ("updated", made["filament_id"])
    assert (again["colour"], again["alpha"], again["colour_source"]) == ("#EEF3F4", 255, "manual")
    book = client.get(BOOK).json()["filaments"]
    assert [(f["id"], f["alpha"], f["cost_per_kg"]) for f in book] == [
        (made["filament_id"], 255, "23")
    ]

    # An item print of the record takes its linked clear spool (the fullest).
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    orders = state.components.get(ORDERS)
    record = orders.store.filament(made["filament_id"])
    assert record is not None
    links = orders.store.spool_links([record.id])
    spools = shelf_spools(state.components.get(FILAMENT).inventory.rows())
    assert {s.rgba for s in spools} == {"#00000000"}
    assert fill_plan({0: record}, spools, None, {}, links) == {1: 9 + OFFSET}
    # The link keeps the spools' own `#RRGGBBAA` (fix round 2, I3).
    assert links[record.id].spool_colour == "#00000000"

    # Every linked spool gone, a new clear spool: found by the link's colour, which is
    # the spools' own `#00000000`, not the record's `#000000FF`. No second record.
    _inventory(client, spool(12, **CLEAR, cost_per_kg=24))
    [newest] = client.post(IMPORT).json()["rows"]
    assert (newest["action"], newest["filament_id"]) == ("updated", made["filament_id"])
    assert [f["id"] for f in client.get(BOOK).json()["filaments"]] == [made["filament_id"]]
    links = orders.store.spool_links([record.id])
    spools = shelf_spools(state.components.get(FILAMENT).inventory.rows())
    assert fill_plan({0: record}, spools, None, {}, links) == {1: 12 + OFFSET}


#: Fix round 2 (I3): `spool_colour` becomes the spools' `#RRGGBBAA`.
RGBA_LINK = (
    Path(__file__).parents[2] / "scadbuddy/migrations/20261116T0300Z_filament_spool_colour_rgba.sql"
)


def _run_updates(conninfo: str, migration: Path) -> None:
    """Run ``migration``'s UPDATE statements again, as on a database from before it."""
    sql = "\n".join(
        line
        for line in migration.read_text(encoding="utf-8").splitlines()
        if not line.startswith("--")
    )
    with psycopg.connect(conninfo, autocommit=True) as conn:
        for statement in sql.split(";"):
            if statement.strip().startswith("UPDATE"):
                conn.execute(statement.encode())


def test_the_rgba_link_migration_keeps_a_stand_ins_clear_spools(
    client: TestClient, pg_conninfo: str
) -> None:
    """Fix round 2 (I3): a prod stand-in from before the migration (alpha 255, the old
    7-char `spool_colour` `#000000`, its linked spools archived) gets `#00000000`, so a new
    clear spool updates it and the import makes no second record; another record's old
    link takes the record's alpha; a link in the new form is left alone."""
    stand_in = "a324599b-86dd-45fe-8408-579491da2f59"
    made = add_filament(client, subtype="Translucent", colour="#EEF3F4", cost_per_kg="20")
    red = add_filament(client, colour="#FF0000", alpha=128, cost_per_kg="20")
    blue = add_filament(client, colour="#0000FF", cost_per_kg="20")
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE filaments SET id = %s, colour_source = 'manual', spool_colour = '#000000',"
            " spool_ids = '{}' WHERE id = %s",
            (stand_in, made["id"]),
        )
        conn.execute("UPDATE filaments SET spool_colour = '#FF0000' WHERE id = %s", (red["id"],))
        conn.execute("UPDATE filaments SET spool_colour = '#0000EEFF' WHERE id = %s", (blue["id"],))

    _run_updates(pg_conninfo, RGBA_LINK)
    _run_updates(pg_conninfo, RGBA_LINK)

    with psycopg.connect(pg_conninfo) as conn:
        links: dict[str, str] = dict(
            conn.execute("SELECT id::text, spool_colour FROM filaments").fetchall()
        )
    assert (links[stand_in], links[red["id"]], links[blue["id"]]) == (
        "#00000000",
        "#FF000080",
        "#0000EEFF",
    )
    _inventory(client, spool(12, **CLEAR, cost_per_kg=24))
    rows = {r["filament_id"]: r for r in client.post(IMPORT).json()["rows"]}
    assert rows[stand_in]["action"] == "updated"
    assert len(client.get(BOOK).json()["filaments"]) == 3


def test_an_old_7_char_link_reads_with_the_records_alpha(
    client: TestClient, pg_conninfo: str
) -> None:
    """Fix rounds 2 and 3 (I3): for one rollout window an old API still writes `#RRGGBB`;
    the new code reads it with the record's alpha (here an opaque record's `FF`), so it
    finds the opaque spools and never the translucent one."""
    made = add_filament(client, colour="#123456", cost_per_kg="20")
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE filaments SET colour_source = 'manual', spool_colour = '#00FF00' WHERE id = %s",
            (made["id"],),
        )
    green = {"rgba": "00FF00FF", "color_name": "Green"}
    glass = {"rgba": "00FF0080", "color_name": "Green Glass"}
    _inventory(client, spool(1, **green, cost_per_kg=25), spool(2, **glass, cost_per_kg=30))

    rows = {_ids(r): r for r in client.post(IMPORT).json()["rows"]}

    assert (rows[(1,)]["action"], rows[(1,)]["filament_id"]) == ("updated", made["id"])
    assert rows[(2,)]["action"] == "created"


def test_the_import_fills_the_look_of_an_imported_record(client: TestClient) -> None:
    """#6209: the backfill. A record whose colour and alpha are the import's takes the
    spool's stops and effect on the next import; handing a colour back (``colour_source``
    ``import``) lets it."""
    old = add_filament(
        client, subtype="Translucent", colour_name="Clear", colour="#000000", alpha=0
    )
    client.patch(f"{BOOK}/filaments/{old['id']}", json={"colour_source": "import"})
    _inventory(client, spool(3, **CLEAR, effect_type="translucent", cost_per_kg=old["cost_per_kg"]))

    row = client.post(IMPORT).json()["rows"][0]

    assert (row["action"], row["filament_id"], row["alpha"], row["effect"]) == (
        "updated",
        old["id"],
        0,
        "translucent",
    )
    assert client.post(IMPORT).json()["rows"][0]["action"] == "unchanged"


def test_a_spool_with_a_corrected_material_is_another_filaments(client: TestClient) -> None:
    """#6211 review: the spool link matches only a record of the group's material, subtype
    and brand, so a spool whose material was corrected never takes over its old record."""
    red = {"rgba": "FF0000FF", "color_name": "Red"}
    _inventory(client, spool(1, **red, cost_per_kg=20), spool(2, **red, cost_per_kg=21))
    made = client.post(IMPORT).json()["rows"][0]
    assert (made["action"], list(_ids(made))) == ("created", [1, 2])

    _inventory(
        client, spool(1, **red, cost_per_kg=20), spool(2, **red, material="PETG", cost_per_kg=30)
    )
    rows = {_ids(r): r for r in client.post(IMPORT).json()["rows"]}

    assert (rows[(1,)]["action"], rows[(1,)]["filament_id"]) == ("updated", made["filament_id"])
    assert rows[(1,)]["cost_per_kg"] == "20"
    assert rows[(2,)]["action"] == "created"
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    old = book[made["filament_id"]]
    assert (old["material"], old["cost_per_kg"]) == ("PLA", "20")
    assert book[rows[(2,)]["filament_id"]]["material"] == "PETG"


def test_a_split_group_never_takes_over_its_record(client: TestClient) -> None:
    """#6211 review: one spool's colour corrected in Bambuddy splits a record's spools. The
    record stays with the spools of its RGB, though the moved spool's group sorts first."""
    red = {"rgba": "FF0000FF", "color_name": "Red"}
    _inventory(client, *(spool(i, **red, cost_per_kg=20) for i in (1, 2, 3)))
    made = client.post(IMPORT).json()["rows"][0]

    blue = {"rgba": "0000FFFF", "color_name": "Blue"}
    _inventory(
        client,
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


def test_a_record_from_before_the_link_follows_a_colour_fix(
    client: TestClient, pg_conninfo: str
) -> None:
    """#6211 review: the migration's backfill, run on a record a person added by hand,
    links it to no spools, so a colour fixed later is the one its spools are found by,
    never the typo's."""
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
    _inventory(client, spool(1, **blue, cost_per_kg=25), spool(2, **typo, cost_per_kg=30))

    rows = {_ids(r): r for r in client.post(IMPORT).json()["rows"]}

    assert (rows[(1,)]["action"], rows[(1,)]["filament_id"]) == ("updated", made["id"])
    assert rows[(2,)]["action"] == "created"
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    assert (book[made["id"]]["colour"], book[made["id"]]["cost_per_kg"]) == ("#0000FF", "25")


def test_a_spool_link_wins_over_a_colour_in_any_group_order(client: TestClient) -> None:
    """#6211 review: a record's one spool recoloured, and a new spool of its old colour.
    The record follows its spool (the link), whichever group sorts first; the new spool
    is a new record."""
    blue = {"rgba": "0000FFFF", "color_name": "Blue"}
    red = {"rgba": "FF0000FF", "color_name": "Red"}
    _inventory(client, spool(1, **blue, cost_per_kg=20))
    made = client.post(IMPORT).json()["rows"][0]

    _inventory(client, spool(1, **red, cost_per_kg=20), spool(2, **blue, cost_per_kg=25))
    rows = client.post(IMPORT).json()["rows"]

    assert [r["colour"] for r in rows] == ["#0000FF", "#FF0000"]
    blue_row, red_row = rows
    assert (red_row["action"], red_row["filament_id"]) == ("updated", made["filament_id"])
    assert (blue_row["action"], list(_ids(blue_row))) == ("created", [2])
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    assert book[made["filament_id"]]["colour"] == "#FF0000"
    assert book[blue_row["filament_id"]]["colour"] == "#0000FF"


def test_a_record_moves_to_a_colour_another_record_leaves_in_one_import(
    client: TestClient,
) -> None:
    """#6211 review: A's spool takes B's colour while B's spool takes a new one. B moves
    first, so A takes its colour in the same import, though A's group sorts first."""
    red = {"rgba": "FF0000FF", "color_name": "Red"}
    blue = {"rgba": "0000FFFF", "color_name": "Blue"}
    green = {"rgba": "00FF00FF", "color_name": "Green"}
    _inventory(client, spool(1, **red, cost_per_kg=20), spool(2, **blue, cost_per_kg=20))
    made = {r["colour"]: r["filament_id"] for r in client.post(IMPORT).json()["rows"]}

    _inventory(client, spool(1, **blue, cost_per_kg=20), spool(2, **green, cost_per_kg=20))
    rows = client.post(IMPORT).json()["rows"]

    assert [(r["colour"], r["action"], r["reason"]) for r in rows] == [
        ("#0000FF", "updated", None),
        ("#00FF00", "updated", None),
    ]
    book = {f["id"]: f["colour"] for f in client.get(BOOK).json()["filaments"]}
    assert book == {made["#FF0000"]: "#0000FF", made["#0000FF"]: "#00FF00"}


def test_a_group_keeps_the_linked_record_of_its_colour(client: TestClient) -> None:
    """#6211 review: a group linked to two records goes to the one whose spools had its
    RGB, though the other is linked to more of its spools."""
    red = {"rgba": "FF0000FF", "color_name": "Red"}
    blue = {"rgba": "0000FFFF", "color_name": "Blue"}
    _inventory(
        client,
        spool(1, **red, cost_per_kg=20),
        spool(2, **red, cost_per_kg=20),
        spool(3, **blue, cost_per_kg=20),
    )
    made = {r["colour"]: r["filament_id"] for r in client.post(IMPORT).json()["rows"]}

    _inventory(client, *(spool(i, **blue, cost_per_kg=30) for i in (1, 2, 3)))
    [row] = client.post(IMPORT).json()["rows"]

    assert (row["filament_id"], list(_ids(row))) == (made["#0000FF"], [1, 2, 3])


def test_two_records_whose_spools_swap_colours_both_move_in_one_import(
    client: TestClient,
) -> None:
    """#6211 review: A (#111111) and B (#222222), whose spools swap colours in Bambuddy,
    both move in one import, with no duplicate and no `colour_taken`."""
    one = {"rgba": "111111FF", "color_name": "Dark"}
    two = {"rgba": "222222FF", "color_name": "Darker"}
    _inventory(client, spool(1, **one, cost_per_kg=20), spool(2, **two, cost_per_kg=20))
    made = {r["colour"]: r["filament_id"] for r in client.post(IMPORT).json()["rows"]}

    _inventory(client, spool(1, **two, cost_per_kg=20), spool(2, **one, cost_per_kg=20))
    rows = client.post(IMPORT).json()["rows"]

    assert [(r["filament_id"], r["action"], r["reason"]) for r in rows] == [
        (made["#222222"], "updated", None),
        (made["#111111"], "updated", None),
    ]
    book = {f["id"]: f["colour"] for f in client.get(BOOK).json()["filaments"]}
    assert book == {made["#111111"]: "#222222", made["#222222"]: "#111111"}
    assert client.post(IMPORT).json()["rows"][0]["action"] == "unchanged"


def test_a_record_held_by_a_hand_set_colour_holds_the_chain_behind_it(
    client: TestClient,
) -> None:
    """#6211 review: a record whose new colour a hand-set record holds keeps its look,
    and so does a record that would move onto its colour."""
    one = {"rgba": "111111FF", "color_name": "Dark"}
    two = {"rgba": "222222FF", "color_name": "Darker"}
    three = {"rgba": "333333FF", "color_name": "Darkest"}
    _inventory(client, spool(1, **one, cost_per_kg=20), spool(2, **two, cost_per_kg=20))
    made = {r["colour"]: r["filament_id"] for r in client.post(IMPORT).json()["rows"]}
    kept = add_filament(client, colour="#333333", colour_name="Mine", cost_per_kg="5")

    # Spool 1 takes the hand-set record's colour; spool 2 takes spool 1's old one.
    _inventory(client, spool(1, **three, cost_per_kg=20), spool(2, **one, cost_per_kg=20))
    rows = {r["filament_id"]: r for r in client.post(IMPORT).json()["rows"]}

    assert rows[made["#111111"]]["reason"] == "colour_taken"
    assert rows[made["#222222"]]["reason"] == "colour_taken"
    book = {f["id"]: f["colour"] for f in client.get(BOOK).json()["filaments"]}
    assert book == {made["#111111"]: "#111111", made["#222222"]: "#222222", kept["id"]: "#333333"}
