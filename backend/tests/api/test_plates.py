from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app


def test_a_bambuddy_model_resolves_to_its_plate(client: TestClient) -> None:
    """#81: Bambuddy's ``Printer.model`` for an H2C is ``"H2C"``."""
    response = client.get("/api/v1/plate", params={"model": "H2C"})
    assert response.status_code == 200
    assert response.json() == {
        "model": "Bambu Lab H2C",
        "name": "H2C",
        "size": [330.0, 320.0],
        "height": 325.0,
        # Where both extruders reach: extruder 1 stops at 325, extruder 2 starts at 25.
        "usable": {"min_x": 25.0, "min_y": 0.0, "max_x": 325.0, "max_y": 320.0},
    }


def test_no_printer_is_the_fallback_plate(client: TestClient) -> None:
    body = client.get("/api/v1/plate").json()
    assert body["model"] is None
    assert body["size"] == [256.0, 256.0]
    assert body["height"] == 250.0


def test_an_unknown_model_is_the_fallback_plate(client: TestClient) -> None:
    assert client.get("/api/v1/plate", params={"model": "Ender 3"}).json()["model"] is None


def test_the_fallback_plate_is_configurable(client: TestClient) -> None:
    client.put("/api/v1/settings", json={"default_plate": "A1M"})
    assert client.get("/api/v1/settings").json()["default_plate"] == "A1M"

    fallback = client.get("/api/v1/plate").json()
    assert fallback["model"] == "Bambu Lab A1 mini"
    assert fallback["size"] == [180.0, 180.0]
    # A printer that IS known still wins over the configured default.
    assert client.get("/api/v1/plate", params={"model": "H2C"}).json()["name"] == "H2C"
    assert client.get("/api/v1/plate", params={"model": "Ender 3"}).json()["name"] == "A1 mini"


def test_the_fallback_plate_can_come_from_the_environment(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"default_plate": "P1S"}))
    with TestClient(app) as client:
        assert client.get("/api/v1/plate").json()["model"] == "Bambu Lab P1S"


def test_the_catalogue_lists_every_known_plate(client: TestClient) -> None:
    body = client.get("/api/v1/plates").json()
    names = [plate["name"] for plate in body["plates"]]
    assert "H2C" in names and "A1 mini" in names
    assert body["default"]["model"] is None


def test_clearing_the_default_plate_outlasts_the_environment(settings: Settings) -> None:
    """An explicit clear is stored, and beats ``SCADBUDDY_DEFAULT_PLATE`` on every later
    read — including after a restart — rather than the environment reappearing."""
    seeded = settings.model_copy(update={"default_plate": "H2C"})
    with TestClient(create_app(seeded)) as client:
        assert client.get("/api/v1/plate").json()["name"] == "H2C"
        assert client.put("/api/v1/settings", json={"default_plate": None}).status_code == 200
        assert client.get("/api/v1/settings").json()["default_plate"] is None
        assert client.get("/api/v1/plate").json()["model"] is None
    with TestClient(create_app(seeded)) as client:
        assert client.get("/api/v1/plate").json()["model"] is None
        # Setting a value again is a value like any other.
        client.put("/api/v1/settings", json={"default_plate": "A1M"})
        assert client.get("/api/v1/plate").json()["name"] == "A1 mini"


def test_a_field_never_written_still_follows_the_environment(settings: Settings) -> None:
    """Saving an unrelated field must not freeze an unset env-backed one as null."""
    with TestClient(create_app(settings)) as client:
        client.put("/api/v1/settings", json={"printer_id": 3})
    seeded = settings.model_copy(update={"default_plate": "P1S"})
    with TestClient(create_app(seeded)) as client:
        assert client.get("/api/v1/plate").json()["name"] == "P1S"


def _fit(client: TestClient, **query: object) -> dict[str, Any]:
    response = client.get("/api/v1/plate/fit", params=query)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def test_a_model_that_fits_has_nothing_to_report(client: TestClient) -> None:
    body = _fit(client, model="H2C", x=100, y=100, z=10, colours=2)
    assert body["overshoots"] == []
    assert body["problem"] is None
    assert body["plate"]["name"] == "H2C"


def test_an_overshoot_names_the_axis_against_the_reachable_area(client: TestClient) -> None:
    body = _fit(client, model="H2C", x=312.1, y=100, z=400)
    assert body["overshoots"] == [
        {"axis": "X", "size": 312.1, "limit": 300.0},
        {"axis": "Z", "size": 400.0, "limit": 325.0},
    ]


def test_no_room_for_the_prime_tower_is_reported_although_the_box_fits(
    client: TestClient,
) -> None:
    """Inside the H2C's 300 x 320 mm reachable area, but a two-colour print needs the
    prime tower as well — which is what ``place_on_plate`` refuses at send time."""
    two_colours = _fit(client, model="H2C", x=295, y=315, z=10, colours=2)
    assert two_colours["overshoots"] == []
    assert "prime tower" in two_colours["problem"]
    # One colour needs no tower, so the same box fits.
    assert _fit(client, model="H2C", x=295, y=315, z=10, colours=1)["problem"] is None


def test_the_filament_cutter_is_reported(client: TestClient) -> None:
    """The X1's cutter corner: a bed-filling single-colour model cannot avoid it."""
    body = _fit(client, model="X1C", x=256, y=256, z=10, colours=1)
    assert body["overshoots"] == []
    assert "filament cutter" in body["problem"]
