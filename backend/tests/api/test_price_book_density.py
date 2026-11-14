"""A price-book filament's density from Bambuddy's link to its slicer preset (#6136):
the spool import and "Fill densities" write it where a record has none, the book says
where each came from, and the estimate says which density it used."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api import orders as orders_api
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.orders import bambuddy as orders_bambuddy
from scadbuddy.orders import component as orders_component
from scadbuddy.slicing.profiles import ProfileImageError
from tests.api.test_order_estimate import ESTIMATE, box, output_of
from tests.api.test_price_book import BOOK, add_filament
from tests.api.test_price_book_spools import IMPORT, spool
from tests.api.test_send import API, configure
from tests.orders.test_density import PROFILES

FILL = f"{BOOK}/fill-densities"
SILK = "Bambu PLA Silk @base (spool preset GFA05)"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"slicer_profiles_dir": PROFILES})


def bambuddy(spools: list[dict[str, Any]], **reads: Any) -> dict[str, respx.Route]:
    """Bambuddy's reads the density chain makes; ``reads`` overrides a body by name."""
    bodies: dict[str, Any] = {
        "local": {"filament": [], "printer": [], "process": []},
        "catalogue": [],
        "assignments": [],
        "status": {"id": 1, "name": "H2C", "connected": True},
        **reads,
    }
    return {
        "spools": respx.get(f"{API}/inventory/spools").mock(
            return_value=httpx.Response(200, json=spools)
        ),
        "local": respx.get(f"{API}/local-presets/").mock(
            return_value=httpx.Response(200, json=bodies["local"])
        ),
        "catalogue": respx.get(f"{API}/filament-catalog/").mock(
            return_value=httpx.Response(200, json=bodies["catalogue"])
        ),
        "assignments": respx.get(f"{API}/inventory/assignments").mock(
            return_value=httpx.Response(200, json=bodies["assignments"])
        ),
        "status": respx.get(f"{API}/printers/1/status").mock(
            return_value=httpx.Response(200, json=bodies["status"])
        ),
    }


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


def test_fill_needs_bambuddy(client: TestClient) -> None:
    response = client.post(FILL)
    assert response.status_code == 409
    assert response.json()["type"].endswith("/not_linked")


@respx.mock
def test_fill_densities_writes_the_empty_ones_only(client: TestClient) -> None:
    # Added before Bambuddy is linked, so born empty.
    empty = tri_color(client)
    own = add_filament(client, colour="#000000", density_g_cm3="1.3")
    alone = tri_color(client, colour="#0000FF")
    configure(client)
    bambuddy(
        [
            # A Tri Color spool names Bambu PLA Silk: its subtype is never read.
            spool(
                1, brand="Bambu Lab", subtype="Tri Color", rgba="00FF00FF", slicer_filament="GFA05"
            ),
            spool(2, rgba="000000FF", slicer_filament="GFA00"),
        ]
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
    routes = bambuddy(
        [
            # Matches `own`: its density stays.
            spool(1, brand="Bambu Lab", cost_per_kg=20, slicer_filament="GFA00"),
            # A K-profile's preset when the spool names none.
            spool(
                2,
                brand="Bambu Lab",
                subtype="Tri Color",
                rgba="00FF00FF",
                cost_per_kg=26,
                k_profiles=[{"setting_id": "GFSA05"}],
            ),
            # A local preset by its numeric id.
            spool(
                3,
                brand="Cookiecad",
                subtype="Magic",
                rgba="FF00FFFF",
                cost_per_kg=30,
                slicer_filament="2",
            ),
            # The AMS tray it is loaded in.
            spool(4, brand="Bambu Lab", rgba="0000FFFF", cost_per_kg=20),
            # Only the catalogue knows it.
            spool(5, brand="Insignia", material="PETG", rgba="111111FF", cost_per_kg=15),
            # Nothing knows it.
            spool(6, brand="Inland", subtype="Glow", rgba="222222FF", cost_per_kg=15),
        ],
        local={
            "filament": [
                {
                    "id": 2,
                    "name": "Cookiecad PETG Magic @H2C",
                    "preset_type": "filament",
                    "source": "orcaslicer",
                    "filament_density": "1.275",
                }
            ],
            "printer": [],
            "process": [],
        },
        catalogue=[
            {
                "id": 1,
                # The spool's subtype, Basic, is a word of the entry's name.
                "name": "Insignia PETG Basic",
                "type": "PETG",
                "brand": "Insignia",
                "color_hex": "#111111",
                "density": 1.27,
            },
        ],
        assignments=[
            {"id": 1, "spool_id": 4, "printer_id": 1, "ams_id": 0, "tray_id": 2},
            {"id": 2, "spool_id": 6, "printer_id": 1, "ams_id": 0, "tray_id": 3},
        ],
        status={
            "id": 1,
            "name": "H2C",
            "connected": True,
            "ams": [
                {
                    "id": 0,
                    "tray": [
                        {"id": 2, "tray_type": "PLA", "tray_info_idx": "GFA00"},
                        {"id": 3, "tray_type": "PLA", "tray_info_idx": ""},
                    ],
                }
            ],
        },
    )

    response = client.post(IMPORT)

    assert response.status_code == 200, response.text
    rows = {row["spool_ids"][0]: row for row in response.json()["rows"]}
    assert (rows[1]["density_g_cm3"], rows[1]["density_profile"]) == ("1.3", None)
    assert (rows[2]["density_g_cm3"], rows[2]["density_profile"]) == (
        "1.32",
        "Bambu PLA Silk @BBL X1C (K-profile preset GFSA05)",
    )
    assert (rows[3]["density_g_cm3"], rows[3]["density_profile"]) == (
        "1.275",
        "Bambuddy local preset Cookiecad PETG Magic @H2C (spool preset 2)",
    )
    assert (rows[4]["density_g_cm3"], rows[4]["density_profile"]) == (
        "1.26",
        "Bambu PLA Basic @base (AMS tray preset GFA00)",
    )
    assert (rows[5]["density_g_cm3"], rows[5]["density_profile"]) == (
        "1.27",
        "Bambuddy filament catalogue",
    )
    assert (rows[6]["action"], rows[6]["density_g_cm3"]) == ("created", None)
    assert {f["id"]: f for f in client.get(BOOK).json()["filaments"]}[own["id"]][
        "density_g_cm3"
    ] == "1.3"
    # One status read, for the one printer that holds a spool naming no preset.
    assert routes["status"].call_count == 1


@respx.mock
def test_no_local_or_tray_read_when_every_spool_names_a_bambu_preset(client: TestClient) -> None:
    configure(client)
    routes = bambuddy([spool(1, cost_per_kg=20, slicer_filament="GFA00")])
    assert client.post(IMPORT).status_code == 200
    assert (routes["local"].call_count, routes["assignments"].call_count) == (0, 0)


@respx.mock
def test_a_filament_added_by_hand_takes_its_spools_density(client: TestClient) -> None:
    configure(client)
    routes = bambuddy(
        [
            spool(
                1,
                brand="Bambu Lab",
                subtype="Tri Color",
                rgba="00FF00FF",
                cost_per_kg=26,
                slicer_filament="GFA05",
            )
        ]
    )
    made = tri_color(client)
    assert (made["density_g_cm3"], made["density_profile"]) == ("1.32", SILK)
    # An import that keeps the record's density reports where it came from.
    [kept] = client.post(IMPORT).json()["rows"]
    assert (kept["density_g_cm3"], kept["density_profile"]) == ("1.32", SILK)
    # A density sent is the person's; no read.
    calls = routes["spools"].call_count
    own = tri_color(client, colour="#0000FF", density_g_cm3_override="1.3")
    assert (own["density_g_cm3"], own["density_profile"]) == ("1.3", None)
    assert routes["spools"].call_count == calls
    # Bambuddy not answering never stops the add: born empty.
    routes["spools"].mock(return_value=httpx.Response(503))
    empty = tri_color(client, colour="#FF0000")
    assert empty["density_g_cm3"] is None
    # Nor does an answer of another shape.
    routes["spools"].mock(return_value=httpx.Response(200, json=[{"material": "PLA"}]))
    assert tri_color(client, colour="#FFFF00")["density_g_cm3"] is None


@respx.mock
def test_the_import_goes_on_with_no_density_when_a_density_read_fails(
    client: TestClient,
) -> None:
    """The density reads are an extra: one Bambuddy refuses leaves the density empty,
    never fails the import of the spools' costs."""
    configure(client)
    routes = bambuddy(
        [
            spool(1, brand="Bambu Lab", cost_per_kg=20, slicer_filament="7"),
            spool(2, brand="Bambu Lab", rgba="0000FFFF", cost_per_kg=20),
        ]
    )
    routes["local"].mock(return_value=httpx.Response(403))
    routes["assignments"].mock(return_value=httpx.Response(503))
    # An answer of another shape is no density either.
    routes["catalogue"].mock(return_value=httpx.Response(200, json=[{"density": 1.24}]))
    response = client.post(IMPORT)
    assert response.status_code == 200, response.text
    assert [(row["action"], row["density_g_cm3"]) for row in response.json()["rows"]] == [
        ("created", None),
        ("created", None),
    ]
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


BASIC = "Bambu PLA Basic @base (spool preset GFA00)"


@respx.mock
def test_an_import_that_fills_only_the_density_is_an_update(client: TestClient) -> None:
    """#6148 review: an unchanged price with an empty density filled is ``updated``,
    with only the density written; a later price change keeps where it came from."""
    empty = add_filament(client, cost_per_kg="20", density_g_cm3=None)
    configure(client)
    routes = bambuddy([spool(1, cost_per_kg=20, slicer_filament="GFA00")])

    [filled] = client.post(IMPORT).json()["rows"]

    assert (filled["action"], filled["cost_per_kg"], filled["previous_cost_per_kg"]) == (
        "updated",
        "20",
        "20",
    )
    assert (filled["density_g_cm3"], filled["density_profile"]) == ("1.26", BASIC)

    routes["spools"].mock(
        return_value=httpx.Response(200, json=[spool(1, cost_per_kg=22, slicer_filament="GFA00")])
    )
    [priced] = client.post(IMPORT).json()["rows"]
    assert (priced["action"], priced["cost_per_kg"]) == ("updated", "22")
    assert (priced["density_g_cm3"], priced["density_profile"]) == ("1.26", BASIC)
    book = {f["id"]: f for f in client.get(BOOK).json()["filaments"]}
    assert (book[empty["id"]]["density_g_cm3"], book[empty["id"]]["density_profile"]) == (
        "1.26",
        BASIC,
    )


@respx.mock
def test_a_slow_bambuddy_never_holds_up_an_add(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6148 review: the density lookup of an add has one deadline; past it the record
    is made with no density."""
    configure(client)
    routes = bambuddy([])

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=[])

    routes["spools"].mock(side_effect=slow)
    monkeypatch.setattr(orders_api, "DENSITY_LOOKUP_TIMEOUT", 0.2)

    started = time.monotonic()
    made = tri_color(client)

    assert time.monotonic() - started < 3
    assert (made["density_g_cm3"], made["density_profile"]) == (None, None)


@respx.mock
@pytest.mark.parametrize(
    "error",
    [
        ProfileImageError("the profile file BBL.json cannot be read"),
        # A tree with BBL.json but no VERSION file (`Profiles.version`).
        FileNotFoundError("VERSION"),
    ],
)
def test_an_unreadable_vendor_tree_leaves_bambuddys_sources(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """#6148 review: a vendor tree that cannot be read is no density from it; the add,
    the import and the fill go on with Bambuddy's own sources."""

    def broken(root: object) -> None:
        raise error

    monkeypatch.setattr(orders_component, "density_table", broken)
    configure(client)
    bambuddy(
        [
            # Its preset is a vendor one, which the broken tree cannot give.
            spool(1, cost_per_kg=20, slicer_filament="GFA00"),
            spool(2, brand="Insignia", material="PETG", rgba="111111FF", cost_per_kg=15),
        ],
        catalogue=[
            {
                "id": 1,
                "name": "Insignia PETG Basic",
                "type": "PETG",
                "brand": "Insignia",
                "color_hex": "#111111",
                "density": 1.27,
            },
        ],
    )

    made = add_filament(client, density_g_cm3=None)
    assert made["density_g_cm3"] is None
    response = client.post(IMPORT)
    assert response.status_code == 200, response.text
    rows = {row["spool_ids"][0]: row for row in response.json()["rows"]}
    assert rows[1]["density_g_cm3"] is None
    assert (rows[2]["density_g_cm3"], rows[2]["density_profile"]) == (
        "1.27",
        "Bambuddy filament catalogue",
    )
    filled = client.post(FILL)
    assert filled.status_code == 200, filled.text
    assert [row["density_g_cm3"] for row in filled.json()["rows"]] == [None]


@respx.mock
def test_a_printer_bambuddy_cannot_read_keeps_the_other_printers_trays(
    client: TestClient,
) -> None:
    """#6148 review: the printers' status reads run at once; one Bambuddy refuses gives
    no tray, and the trays of the printers that answered are still read."""
    configure(client)
    bambuddy(
        [
            spool(1, cost_per_kg=20),
            spool(2, rgba="0000FFFF", cost_per_kg=20),
        ],
        assignments=[
            {"id": 1, "spool_id": 1, "printer_id": 1, "ams_id": 0, "tray_id": 0},
            {"id": 2, "spool_id": 2, "printer_id": 2, "ams_id": 0, "tray_id": 0},
        ],
        status={
            "id": 1,
            "name": "H2C",
            "connected": True,
            "ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_info_idx": "GFA00"}]}],
        },
    )
    respx.get(f"{API}/printers/2/status").mock(return_value=httpx.Response(503))

    response = client.post(IMPORT)

    assert response.status_code == 200, response.text
    rows = {row["spool_ids"][0]: row for row in response.json()["rows"]}
    assert (rows[1]["density_g_cm3"], rows[1]["density_profile"]) == (
        "1.26",
        "Bambu PLA Basic @base (AMS tray preset GFA00)",
    )
    assert (rows[2]["action"], rows[2]["density_g_cm3"]) == ("created", None)


@respx.mock
def test_an_unexpected_density_read_error_is_raised_as_itself(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6186 review: an error that is not Bambuddy's refusal reaches the caller as
    itself, not wrapped in the reads' ExceptionGroup."""
    configure(client)
    bambuddy([spool(1, cost_per_kg=20)])

    async def broken(*args: object) -> dict[int, str]:
        try:
            raise KeyError("decode")
        except KeyError as error:
            raise RuntimeError("tray read broke") from error

    monkeypatch.setattr(orders_bambuddy, "_tray_presets", broken)
    with pytest.raises(RuntimeError, match="tray read broke") as raised:
        client.post(IMPORT)
    # Its own cause stays (#6188 review).
    assert isinstance(raised.value.__cause__, KeyError)


@respx.mock
def test_when_two_density_reads_fail_one_is_raised_and_the_other_logged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#6188 review: the first failed read is raised as itself; another that failed too
    is logged, never dropped."""
    configure(client)
    bambuddy([spool(1, cost_per_kg=20)])

    async def trays_broke(*args: object) -> dict[int, str]:
        raise RuntimeError("tray read broke")

    async def catalogue_broke(self: object) -> list[object]:
        raise RuntimeError("catalogue read broke")

    monkeypatch.setattr(orders_bambuddy, "_tray_presets", trays_broke)
    monkeypatch.setattr(BambuddyClient, "filament_catalog", catalogue_broke)
    with (
        caplog.at_level(logging.WARNING, logger=orders_bambuddy.__name__),
        pytest.raises(RuntimeError, match="read broke") as raised,
    ):
        client.post(IMPORT)

    [logged] = [r for r in caplog.records if r.message == "another density read failed too"]
    assert logged.exc_info is not None
    assert {str(raised.value), str(logged.exc_info[1])} == {
        "tray read broke",
        "catalogue read broke",
    }
