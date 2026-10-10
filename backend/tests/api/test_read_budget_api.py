"""#2087 — what a read of a library 3MF may spend: a setting each, overridable for one
request, never past a ceiling."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import get_render
from scadbuddy.api.outputs import LIBRARY_FILE_NOT_ARRANGEABLE
from scadbuddy.render.read_budget import CEILINGS
from tests.api.test_analyzers import _run_library, bambuddy_routes, library_routes
from tests.api.test_arrange_api import Arranger, two_colour_3mf
from tests.api.test_print_library import library_file
from tests.api.test_send import configure
from tests.support.arrange import saved_output

#: two_colour_3mf's boxes: 12 triangles each.
TRIANGLES = 24


def _hashed(file_id: int, tmp_path: Path) -> None:
    content = two_colour_3mf(tmp_path)
    library_file(file_id, content=content, file_hash=hashlib.sha256(content).hexdigest())


@respx.mock
def test_a_request_can_lower_and_raise_the_setting(client: TestClient, tmp_path: Path) -> None:
    configure(client)
    _hashed(88, tmp_path)
    lowered = client.get(f"/api/v1/print/library/88/objects?max_triangles={TRIANGLES - 1}")
    assert lowered.status_code == 422, lowered.text
    assert lowered.json()["code"] == LIBRARY_FILE_NOT_ARRANGEABLE
    assert "max_triangles read budget" in lowered.json()["detail"]
    # The refusal was kept under the lower budget; the default reads the file again.
    assert client.get("/api/v1/print/library/88/objects").status_code == 200


@respx.mock
def test_the_setting_is_the_default_and_a_request_overrides_it(
    client: TestClient, tmp_path: Path
) -> None:
    configure(client)
    _hashed(88, tmp_path)
    saved = client.put("/api/v1/settings", json={"read_max_triangles": TRIANGLES - 1})
    assert saved.status_code == 200, saved.text
    assert saved.json()["read_max_triangles"] == TRIANGLES - 1
    assert saved.json()["applies"]["read_max_triangles"] == "live"
    refused = client.get("/api/v1/print/library/88/objects")
    assert refused.status_code == 422 and "SCADBUDDY_READ_MAX_TRIANGLES" in refused.text
    raised = client.get(f"/api/v1/print/library/88/objects?max_triangles={TRIANGLES}")
    assert raised.status_code == 200, raised.text


def test_no_setting_goes_past_its_ceiling(client: TestClient) -> None:
    ceiling = CEILINGS["max_triangles"]
    past = client.put("/api/v1/settings", json={"read_max_triangles": ceiling + 1})
    assert past.status_code == 422, past.text
    assert "SCADBUDDY_READ_MAX_TRIANGLES" in past.text
    at = client.put("/api/v1/settings", json={"read_max_triangles": ceiling})
    assert at.status_code == 200, at.text


@respx.mock
def test_no_override_goes_past_its_ceiling(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    configure(client)
    _hashed(88, tmp_path)
    for budget, ceiling in CEILINGS.items():
        past = client.get(f"/api/v1/print/library/88/objects?{budget}={ceiling + 1}")
        assert past.status_code == 422, (budget, past.text)
        assert budget in past.text
    arranger = Arranger()
    app.dependency_overrides[get_render] = lambda: arranger
    body = {
        "objects": [{"library_file_id": 88}],
        "slug": "demo",
        "read_budget": {"max_paint_digits": CEILINGS["max_paint_digits"] + 1},
    }
    past = client.post("/api/v1/outputs/arrange", json=body)
    assert past.status_code == 422, past.text
    assert "max_paint_digits" in past.text and arranger.inputs == []


@respx.mock
def test_an_arrange_reads_within_its_own_budget(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    configure(client)
    asyncio.run(saved_output(tmp_path))  # the template the result is filed under
    _hashed(88, tmp_path)
    arranger = Arranger()
    app.dependency_overrides[get_render] = lambda: arranger
    body = {"objects": [{"library_file_id": 88}], "slug": "demo"}
    small = {**body, "read_budget": {"max_triangles": TRIANGLES - 1}}
    refused = client.post("/api/v1/outputs/arrange", json=small)
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == LIBRARY_FILE_NOT_ARRANGEABLE
    assert client.post("/api/v1/outputs/arrange", json=body).status_code == 202


@respx.mock
def test_a_preview_reads_within_its_own_budget(client: TestClient, tmp_path: Path) -> None:
    configure(client)
    library_file(88, content=two_colour_3mf(tmp_path))
    path = "/api/v1/print/library/88/preview.glb"
    refused = client.get(path, params={"max_triangles": TRIANGLES - 1})
    assert refused.status_code == 422, refused.text
    assert "max_triangles read budget" in refused.json()["detail"]
    assert client.get(path).status_code == 200


@respx.mock
def test_the_print_checks_read_within_their_own_budget(client: TestClient, tmp_path: Path) -> None:
    configure(client)
    bambuddy_routes()
    library_routes(89, two_colour_3mf(tmp_path))

    def geometry(request: dict[str, object]) -> dict[str, object]:
        report = _run_library(client, 89, request=request)
        found: dict[str, object] = next(
            row for row in report["inputs"] if row["name"] == "geometry"
        )
        return found

    small = geometry({"read_budget": {"max_triangles": TRIANGLES - 1}})
    assert not small["available"] and "max_triangles read budget" in str(small["reason"])
    assert geometry({})["available"]
