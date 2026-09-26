from __future__ import annotations

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
