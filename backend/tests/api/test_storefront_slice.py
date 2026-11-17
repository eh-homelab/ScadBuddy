"""The shop slice profile's choices (#6153, #6574): the options route offers the filament
presets the print dialog's Advanced step offers on the printer, read from Bambuddy's
catalogue on its path and from the image's on a native route."""

from __future__ import annotations

from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api import storefront_slice
from scadbuddy.bambuddy.catalogue import Catalogue, PresetChoice
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.models import PrinterCreate
from tests.api.test_settings_runtime import _state
from tests.bambuddy.test_choices_reads import _ok

OPTIONS = "/api/v1/storefront/slice-profile/options"
BAMBUDDY = "https://bambuddy.test/api/v1"


def _printer(client: TestClient) -> str:
    store = _state(client).components.get(PRINTERS).store
    return store.create(PrinterCreate(serial="01P00A0000000AA", name="Garage")).id


@respx.mock
def test_the_options_list_the_dialogs_presets_for_a_printer(client: TestClient) -> None:
    printer = _printer(client)
    client.put("/api/v1/settings", json={"bambuddy_url": "https://bambuddy.test"})
    respx.get(f"{BAMBUDDY}/slicer/presets").mock(return_value=_ok("slicer-presets-h2c.json"))
    respx.get(f"{BAMBUDDY}/local-presets/").mock(return_value=_ok("local-presets.json"))
    answer = client.get(OPTIONS, params={"printer": printer})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["nozzle_size"] == "0.4"
    names = [preset["name"] for preset in body["presets"]]
    assert "Bambu PLA Basic @BBL H2C" in names
    assert names == sorted(names, key=str.lower)


def test_the_options_name_no_preset_without_a_printer(client: TestClient) -> None:
    answer = client.get(OPTIONS)
    assert answer.status_code == 200, answer.text
    assert answer.json()["presets"] == []


def test_the_options_refuse_a_printer_the_registry_lacks(client: TestClient) -> None:
    answer = client.get(OPTIONS, params={"printer": "nowhere"})
    assert answer.status_code == 422


def test_a_native_route_offers_the_images_presets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a native route the presets are the image's, and Bambuddy is not asked (respx
    is not active, so a Bambuddy call would fail)."""
    printer = _printer(client)

    async def a_route(*_: Any, **__: Any) -> Any:
        return object()

    pla = PresetChoice(
        ref=PresetRef(source="standard", id="Bambu PLA Basic @BBL H2C"),
        name="Bambu PLA Basic @BBL H2C",
        filament_type="PLA",
        compatible_printers=["Bambu Lab H2C 0.4 nozzle"],
    )

    async def image_catalogue(*_: Any, **__: Any) -> Catalogue:
        return Catalogue(printer=[], process=[], filament=[pla])

    monkeypatch.setattr(storefront_slice, "dialog_route", a_route)
    monkeypatch.setattr(storefront_slice, "catalogue_for", image_catalogue)
    answer = client.get(OPTIONS, params={"printer": printer})
    assert answer.status_code == 200, answer.text
    assert answer.json()["presets"] == [
        {
            "ref": {"source": "standard", "id": "Bambu PLA Basic @BBL H2C"},
            "name": "Bambu PLA Basic @BBL H2C",
            "filament_type": "PLA",
        }
    ]
