"""The print dialog's routes refresh the kept preset catalogue under the cross-replica
claim (#4528): a route that dropped it would fall back to one refresh per replica with
nothing failing."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api import library_print, printing
from scadbuddy.bambuddy import catalogue
from scadbuddy.core.settings import Settings
from tests.api.test_print import printers_route
from tests.api.test_print_choices import API, h2c_presets
from tests.api.test_print_filaments import inventory_routes, prepared
from tests.api.test_print_library import SLICED_RUN, sliced_file
from tests.api.test_print_run_choices import run_request, run_routes
from tests.api.test_send import configure, make_output, upload_route
from tests.bambuddy.conftest import recording

pytestmark = pytest.mark.requires_postgres


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The conninfo of every claim a route builds; each is the real claim."""
    asked: list[str] = []

    def claim(conninfo: str) -> catalogue.RefreshClaim:
        asked.append(conninfo)
        return catalogue.catalogue_refresh_claim(conninfo)

    # Each route module imported its own name for it.
    monkeypatch.setattr(printing, "catalogue_refresh_claim", claim)
    monkeypatch.setattr(library_print, "catalogue_refresh_claim", claim)
    return asked


@respx.mock
def test_the_choices_refresh_under_the_claim(
    client: TestClient, model: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _spy(monkeypatch)
    output_id = prepared(client, model)
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=recording("printer-status-rack.json"))
    )
    respx.get(f"{API}/archives/").mock(
        return_value=httpx.Response(200, json=recording("archives.json"))
    )

    answer = client.get(f"/api/v1/print/outputs/{output_id}/choices?printer_id=1")

    assert answer.status_code == 200, answer.text
    assert asked == [settings.database_url]


@respx.mock
def test_the_check_refreshes_under_the_claim(
    client: TestClient, model: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _spy(monkeypatch)
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    run_routes()

    answer = client.post(f"/api/v1/print/outputs/{output_id}/check", json=run_request())

    assert answer.status_code == 200, answer.text
    assert asked == [settings.database_url]


@respx.mock
def test_a_library_files_choices_refresh_under_the_claim(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _spy(monkeypatch)
    configure(client)
    sliced_file(104)
    run_routes()

    answer = client.get("/api/v1/print/library/104/choices")

    assert answer.status_code == 200, answer.text
    assert asked == [settings.database_url]


@respx.mock
def test_a_library_files_check_refreshes_under_the_claim(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _spy(monkeypatch)
    configure(client)
    sliced_file(104)
    run_routes()

    answer = client.post("/api/v1/print/library/104/check", json=SLICED_RUN)

    assert answer.status_code == 200, answer.text
    assert asked == [settings.database_url]
