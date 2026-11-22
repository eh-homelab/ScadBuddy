"""The colour catalog's sync routes (#6907): ``POST /filament/colour-syncs`` runs the
``filament_colour_sync`` operation on the library worker, ``GET`` reads each source's
newest run. filamentcolors.xyz is respx (never the real site)."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.filament.colour_catalog import FILAMENTCOLORS_API
from tests.api.test_filament_purchasing import _profiles, _table

pytestmark = pytest.mark.requires_postgres

URL = "/api/v1/filament"


def test_bambu_s_codes_fill_the_catalog_and_the_run_is_listed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _table(client, monkeypatch, _profiles(tmp_path))
    assert client.get(f"{URL}/colour-syncs").json() == []
    done = client.post(
        f"{URL}/colour-syncs", json={"source": "bambu"}, headers={"Idempotency-Key": "b1"}
    )
    assert done.status_code == 200, done.text
    assert (done.json()["state"], done.json()["added"]) == ("done", 4)
    rows = client.get(f"{URL}/catalog/colours", params={"q": "translucent yellow"}).json()
    assert [(r["name"], r["rgba"], r["source"]) for r in rows] == [
        ("Translucent Yellow", "#F4EE2A80", "bambu")
    ]
    (listed,) = client.get(f"{URL}/colour-syncs").json()
    assert (listed["source"], listed["state"]) == ("bambu", "done")


def test_a_filamentcolors_outage_is_a_failed_run_not_an_error(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.route(host="testserver").pass_through()
        router.get(f"{FILAMENTCOLORS_API}/version/").respond(404)
        done = client.post(
            f"{URL}/colour-syncs",
            json={"source": "filamentcolors"},
            headers={"Idempotency-Key": "f1"},
        )
    assert done.status_code == 200, done.text
    assert (done.json()["state"], done.json()["error"]) == (
        "failed",
        "filamentcolors.xyz answered 404",
    )


def test_an_unknown_source_is_422(client: TestClient) -> None:
    refused = client.post(f"{URL}/colour-syncs", json={"source": "pantone"})
    assert refused.status_code == 422, refused.text
