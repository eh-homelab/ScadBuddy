"""A price-book filament's density from ScadBuddy's spools (#6136, #6262 Task 9 review
ruling): the newest spool's own, else its slicer preset's (a ScadBuddy preset's, else the
image's vendor preset's; a Bambuddy local preset is never read, #7244), else that of the
preset the tray holding it reports. The spool import and "Fill densities" write it where a
record has none, the book says where each came from, and the estimate says which density it
used."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from scadbuddy.api import orders as orders_api
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.filament.component import FILAMENT, Filament
from scadbuddy.filament.models import SpoolPatch
from scadbuddy.filament.sb_presets import create_on, document_bytes, keep_document, rebind_on
from scadbuddy.orders import component as orders_component
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.slicing.profiles import ProfileImageError
from tests.api.test_filament import TRAY_UUID
from tests.api.test_order_estimate import ESTIMATE, box, output_of
from tests.api.test_price_book import BOOK, add_filament
from tests.api.test_price_book_spools import IMPORT, OFFSET, _ids, _inventory, spool
from tests.api.test_send import API, configure
from tests.orders.test_density import PROFILES

FILL = f"{BOOK}/fill-densities"
SILK = "Bambu PLA Silk @base (spool preset GFA05)"
BASIC = "Bambu PLA Basic @base (spool preset GFA00)"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"slicer_profiles_dir": PROFILES})


def _filament(client: TestClient) -> Filament:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    found: Filament = state.components.get(FILAMENT)
    return found


def own_density(client: TestClient, number: int, density: str) -> None:
    """The SB spool of Bambuddy id ``number`` (`_inventory` numbers it plus ``OFFSET``)
    with its own density."""
    filament = _filament(client)
    found = filament.inventory.by_number(number + OFFSET)
    assert found is not None, number
    patch = SpoolPatch.model_validate({"density_g_cm3": density})
    assert filament.spools.update(found[0], patch, actor="test") is not None


def local_presets(*presets: dict[str, Any]) -> respx.Route:
    return respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(
            200, json={"filament": list(presets), "printer": [], "process": []}
        )
    )


def tri_color(
    client: TestClient, colour: str = "#00FF00", density_g_cm3_override: str | None = None
) -> dict[str, Any]:
    return add_filament(
        client,
        brand="Bambu Lab",
        subtype="Tri Color",
        colour=colour,
        density_g_cm3=density_g_cm3_override,
    )


def test_fill_needs_no_bambuddy(client: TestClient) -> None:
    response = client.post(FILL)
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == []


def test_fill_densities_writes_the_empty_ones_only(client: TestClient) -> None:
    # Added before the spools, so born empty.
    empty = tri_color(client)
    own = add_filament(client, colour="#000000", density_g_cm3="1.3")
    alone = tri_color(client, colour="#0000FF")
    _inventory(
        client,
        # A Tri Color spool names Bambu PLA Silk: its subtype is never read.
        spool(1, brand="Bambu Lab", subtype="Tri Color", rgba="00FF00FF", slicer_filament="GFA05"),
        spool(2, rgba="000000FF", slicer_filament="GFA00"),
    )

    response = client.post(FILL)

    assert response.status_code == 200, response.text
    rows = {row["filament_id"]: row for row in response.json()["rows"]}
    # `own` keeps its density; `alone` has no spool, so no source.
    assert set(rows) == {empty["id"], alone["id"]}
    assert (rows[empty["id"]]["density_g_cm3"], rows[empty["id"]]["density_profile"]) == (
        "1.32",
        SILK,
    )
    assert rows[alone["id"]]["density_g_cm3"] is None
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    assert (book[empty["id"]]["density_g_cm3"], book[empty["id"]]["density_profile"]) == (
        "1.32",
        SILK,
    )
    assert (book[own["id"]]["density_g_cm3"], book[own["id"]]["density_profile"]) == ("1.3", None)

    # The editor's save sends the same density back: where it came from stays.
    kept = client.patch(
        f"{BOOK}/filaments/{empty['id']}", json={"density_g_cm3": "1.320", "cost_per_kg": "9"}
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["density_profile"] == SILK
    # A person's density replaces it, and names no source.
    edited = client.patch(f"{BOOK}/filaments/{empty['id']}", json={"density_g_cm3": "1.25"})
    assert (edited.json()["density_g_cm3"], edited.json()["density_profile"]) == ("1.25", None)
    assert [row["filament_id"] for row in client.post(FILL).json()["rows"]] == [alone["id"]]


@respx.mock
def test_the_spool_import_follows_the_chain(client: TestClient) -> None:
    configure(client)
    own = add_filament(client, brand="Bambu Lab", density_g_cm3="1.3")
    _inventory(
        client,
        # Matches `own`: its density stays.
        spool(1, brand="Bambu Lab", cost_per_kg=20, slicer_filament="GFA00"),
        # The vendor preset its preset id names.
        spool(
            2,
            brand="Bambu Lab",
            subtype="Tri Color",
            rgba="00FF00FF",
            cost_per_kg=26,
            slicer_filament="GFA05",
        ),
        # A Bambuddy local preset by its numeric id: never read (#7244), so no density.
        spool(
            3,
            brand="Cookiecad",
            subtype="Magic",
            rgba="FF00FFFF",
            cost_per_kg=30,
            slicer_filament="2",
        ),
        # No preset: the spool's own density.
        spool(4, brand="Bambu Lab", rgba="0000FFFF", cost_per_kg=20),
        # Nothing knows it.
        spool(6, brand="Inland", subtype="Glow", rgba="222222FF", cost_per_kg=15),
    )
    own_density(client, 4, "1.27")
    local = local_presets(
        {
            "id": 2,
            "name": "Cookiecad PETG Magic @H2C",
            "preset_type": "filament",
            "source": "orcaslicer",
            "filament_density": "1.275",
        }
    )

    response = client.post(IMPORT)

    assert response.status_code == 200, response.text
    rows = {_ids(row)[0]: row for row in response.json()["rows"]}
    assert (rows[1]["density_g_cm3"], rows[1]["density_profile"]) == ("1.3", None)
    assert (rows[2]["density_g_cm3"], rows[2]["density_profile"]) == ("1.32", SILK)
    assert (rows[3]["density_g_cm3"], rows[3]["density_profile"]) == (None, None)
    assert (rows[4]["density_g_cm3"], rows[4]["density_profile"]) == (
        "1.27",
        "ScadBuddy spool #104",
    )
    assert (rows[6]["action"], rows[6]["density_g_cm3"]) == ("created", None)
    assert {f["id"]: f for f in client.get(BOOK).json()["filaments"]}[own["id"]][
        "density_g_cm3"
    ] == "1.3"
    # No read of Bambuddy's local presets, even for the numeric preset id.
    assert local.call_count == 0


@respx.mock
def test_no_local_read_when_every_spool_names_a_bambu_preset(client: TestClient) -> None:
    configure(client)
    local = local_presets()
    _inventory(client, spool(1, cost_per_kg=20, slicer_filament="GFA00"))
    assert client.post(IMPORT).status_code == 200
    assert local.call_count == 0


def adopted(client: TestClient, pg_conninfo: str, bb_id: int, density: str) -> str:
    """A ScadBuddy preset made from Bambuddy local preset ``bb_id`` (its document's
    ``filament_density`` is ``density``), with every binding that named ``bb_id``
    rebound to it, as `adopt_held` does (#7176); its ref."""
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    name = "Cookiecad PETG Magic @H2C"
    doc: dict[str, Any] = {"name": name, "inherits": "", "filament_density": [density]}
    key, sha256 = asyncio.run(keep_document(state.store.blobs, document_bytes(doc)))
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        preset_id = create_on(
            conn,
            name=name,
            source="bambuddy",
            key=key,
            sha256=sha256,
            doc=doc,
            made_by="test",
            bb_preset_id=bb_id,
        )
        assert rebind_on(conn, bb_id, preset_id) > 0
    return f"sb-{preset_id}"


@respx.mock
def test_an_adopted_preset_gives_its_density(client: TestClient, pg_conninfo: str) -> None:
    """#7176: once a spool's Bambuddy local preset is adopted, its binding names the
    ScadBuddy preset (``sb-<id>``), whose document gives the density; Bambuddy's local
    presets are not read for it."""
    configure(client)
    _inventory(client, spool(3, brand="Cookiecad", cost_per_kg=30, slicer_filament="2"))
    ref = adopted(client, pg_conninfo, 2, "1.275")
    local = local_presets()

    response = client.post(IMPORT)

    assert response.status_code == 200, response.text
    [row] = response.json()["rows"]
    assert (row["density_g_cm3"], row["density_profile"]) == (
        "1.275",
        f"ScadBuddy preset Cookiecad PETG Magic @H2C (spool preset {ref})",
    )
    assert local.call_count == 0


def test_a_spool_with_no_preset_takes_its_trays(client: TestClient, pg_conninfo: str) -> None:
    """Task 9 review ruling: last in the chain, the preset id (``tray_info_idx``) the tray
    a registry printer reports holding the spool in."""
    _inventory(client, spool(1, cost_per_kg=20))
    found = _filament(client).inventory.by_number(1 + OFFSET)
    assert found is not None
    tag = {"tray_uuid": TRAY_UUID, "kind": "bambu_mifare"}
    bound = client.post(f"/api/v1/filament/spools/{found[0]}/tags", json=tag)
    assert bound.status_code == 201, bound.text
    printer = str(uuid4())
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name, lan_host) VALUES (%s, %s, %s, %s)",
            (printer, "SN-SILK", "Silk", "10.0.0.8"),
        )
    tray = {"id": "1", "tray_type": "PLA", "tray_color": "FFFFFFFF", "state": 11}
    tray |= {"tray_uuid": TRAY_UUID, "tray_info_idx": "GFA05"}
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    PrinterReportStore(state.settings_store.pool).put(
        printer, {"ams": {"ams": [{"id": "0", "tray": [tray]}]}}, datetime.now(UTC)
    )

    response = client.post(IMPORT)

    assert response.status_code == 200, response.text
    [row] = response.json()["rows"]
    assert (row["density_g_cm3"], row["density_profile"]) == (
        "1.32",
        "Bambu PLA Silk @base (tray preset GFA05)",
    )


def test_a_spool_read_that_fails_is_a_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ScadBuddy's spools that cannot be read stop the import and the fill with a 503."""

    def down(*args: object, **kwargs: object) -> None:
        raise psycopg.OperationalError("the database is not answering")

    monkeypatch.setattr(type(_filament(client).inventory), "rows", down)
    for route in (IMPORT, FILL):
        response = client.post(route)
        assert response.status_code == 503, (route, response.text)


def test_without_bambuddy_a_local_preset_gives_no_density(client: TestClient) -> None:
    _inventory(client, spool(1, cost_per_kg=20, slicer_filament="2"))
    [row] = client.post(IMPORT).json()["rows"]
    assert (row["action"], row["density_g_cm3"]) == ("created", None)


def test_a_filament_added_by_hand_takes_its_spools_density(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inventory(
        client,
        spool(
            1,
            brand="Bambu Lab",
            subtype="Tri Color",
            rgba="00FF00FF",
            cost_per_kg=26,
            slicer_filament="GFA05",
        ),
    )
    made = tri_color(client)
    assert (made["density_g_cm3"], made["density_profile"]) == ("1.32", SILK)
    # An import that keeps the record's density reports where it came from.
    [kept] = client.post(IMPORT).json()["rows"]
    assert (kept["density_g_cm3"], kept["density_profile"]) == ("1.32", SILK)
    # A density sent is the person's.
    own = tri_color(client, colour="#0000FF", density_g_cm3_override="1.3")
    assert (own["density_g_cm3"], own["density_profile"]) == ("1.3", None)
    # A translucent one of the spool's RGB is another filament: no density from it.
    clear = add_filament(
        client,
        brand="Bambu Lab",
        subtype="Tri Color",
        colour="#00FF00",
        alpha=0x80,
        density_g_cm3=None,
    )
    assert clear["density_g_cm3"] is None
    # Spools that cannot be read never stop the add: born empty.

    async def unread(*args: object, **kwargs: object) -> None:
        raise psycopg.OperationalError("the database is gone")

    monkeypatch.setattr(orders_api, "inventory_groups", unread)
    assert tri_color(client, colour="#FF0000")["density_g_cm3"] is None


@respx.mock
def test_the_import_goes_on_with_no_density_when_the_local_read_fails(
    client: TestClient,
) -> None:
    """The local presets' read is an extra: one Bambuddy refuses leaves the density
    empty, never fails the import of the spools' costs."""
    configure(client)
    _inventory(
        client,
        spool(1, brand="Bambu Lab", cost_per_kg=20, slicer_filament="7"),
        spool(2, brand="Bambu Lab", rgba="0000FFFF", cost_per_kg=20, slicer_filament="8"),
    )
    local = local_presets()
    local.mock(return_value=httpx.Response(403))
    response = client.post(IMPORT)
    assert response.status_code == 200, response.text
    assert [(row["action"], row["density_g_cm3"]) for row in response.json()["rows"]] == [
        ("created", None),
        ("created", None),
    ]
    # An answer of another shape is no density either.
    local.mock(return_value=httpx.Response(200, json={"filament": [{"density": 1.24}]}))
    filled = client.post(FILL)
    assert filled.status_code == 200, filled.text
    assert [row["density_g_cm3"] for row in filled.json()["rows"]] == [None, None]


def test_the_estimate_says_which_density_it_used(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = output_of(client, model, paths, [box(20, 20, 20)])
    own = add_filament(client, density_g_cm3="1.26")
    response = client.get(ESTIMATE, params={"output_id": output_id, "filament_id": own["id"]})
    assert response.status_code == 200, response.text
    # 8 cm3 x 1.26 x 1.15 = 11.592
    assert response.json()["grams"] == "11.59"
    assert response.json()["densities"] == [
        {"filament_id": own["id"], "density_g_cm3": "1.26", "profile": None}
    ]
    none = tri_color(client)
    refused = client.get(ESTIMATE, params={"output_id": output_id, "filament_id": none["id"]})
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"].endswith("/filament_density")
    assert "fill-densities" in refused.json()["detail"]


def test_an_import_that_fills_only_the_density_is_an_update(client: TestClient) -> None:
    """#6148 review: an unchanged price with an empty density filled is ``updated``,
    with only the density written; a later price change keeps where it came from."""
    empty = add_filament(client, cost_per_kg="20", density_g_cm3=None)
    _inventory(client, spool(1, cost_per_kg=20, slicer_filament="GFA00"))

    [filled] = client.post(IMPORT).json()["rows"]

    assert (filled["action"], filled["cost_per_kg"], filled["previous_cost_per_kg"]) == (
        "updated",
        "20",
        "20",
    )
    assert (filled["density_g_cm3"], filled["density_profile"]) == ("1.26", BASIC)

    _inventory(client, spool(1, cost_per_kg=22, slicer_filament="GFA00"))
    [priced] = client.post(IMPORT).json()["rows"]
    assert (priced["action"], priced["cost_per_kg"]) == ("updated", "22")
    assert (priced["density_g_cm3"], priced["density_profile"]) == ("1.26", BASIC)
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    assert (book[empty["id"]]["density_g_cm3"], book[empty["id"]]["density_profile"]) == (
        "1.26",
        BASIC,
    )


def test_a_slow_lookup_never_holds_up_an_add(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6148 review: the density lookup of an add has one deadline; past it the lookup
    is cancelled and the record is made with no density. Checked by the cancel, not by
    a time bound (#6593): a deadline that did not work would let the lookup end on its
    own, and the test would fail on ``cancelled``, however fast the runner."""
    cancelled = False

    async def slow(*args: object, **kwargs: object) -> list[object]:
        nonlocal cancelled
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return []

    monkeypatch.setattr(orders_api, "inventory_groups", slow)
    monkeypatch.setattr(orders_api, "DENSITY_LOOKUP_TIMEOUT", 0.2)

    made = tri_color(client)

    assert cancelled
    assert (made["density_g_cm3"], made["density_profile"]) == (None, None)


@pytest.mark.parametrize(
    "error",
    [
        ProfileImageError("the profile file BBL.json cannot be read"),
        # A tree with BBL.json but no VERSION file (`Profiles.version`).
        FileNotFoundError("VERSION"),
    ],
)
def test_an_unreadable_vendor_tree_leaves_the_spools_own(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """#6148 review: a vendor tree that cannot be read is no density from it; the add,
    the import and the fill go on with the spools' own densities."""

    def broken(root: object) -> None:
        raise error

    monkeypatch.setattr(orders_component, "density_table", broken)
    _inventory(
        client,
        # Its preset is a vendor one, which the broken tree cannot give.
        spool(1, cost_per_kg=20, slicer_filament="GFA00"),
        spool(2, brand="Insignia", material="PETG", rgba="111111FF", cost_per_kg=15),
    )
    own_density(client, 2, "1.27")

    made = add_filament(client, density_g_cm3=None)
    assert made["density_g_cm3"] is None
    response = client.post(IMPORT)
    assert response.status_code == 200, response.text
    rows = {_ids(row)[0]: row for row in response.json()["rows"]}
    assert rows[1]["density_g_cm3"] is None
    assert (rows[2]["density_g_cm3"], rows[2]["density_profile"]) == (
        "1.27",
        "ScadBuddy spool #102",
    )
    filled = client.post(FILL)
    assert filled.status_code == 200, filled.text
    assert [row["density_g_cm3"] for row in filled.json()["rows"]] == [None]
