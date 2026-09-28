"""Issue #317 — Generate with a project files the editable 3MF in its Bambuddy folder.

``POST /outputs/{id}/project-file`` is what the Customize page calls once Generate has
saved an output with a project chosen. It uploads through the same library-copy cache a
print uses (#316), so a later print on the same printer and nozzle reuses that one file.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy.project_file import project_stem
from scadbuddy.render.schema import ParamValue
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import run_request, run_routes
from tests.api.test_send import BASE, configure, make_output

pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"

PROJECT = 7
FOLDER = 9


def project_folder_routes(files: list[dict[str, Any]] | None = None) -> respx.Route:
    """Project 7's folder is 9; the listing the unique name is checked against."""
    respx.get(f"{API}/library/folders/by-project/{PROJECT}").mock(
        return_value=httpx.Response(
            200, json=[{"id": FOLDER, "name": "Kids' room", "project_id": PROJECT}]
        )
    )
    return respx.get(f"{API}/library/files", params={"folder_id": str(FOLDER)}).mock(
        return_value=httpx.Response(200, json=files or [])
    )


def uploads(*ids: int) -> respx.Route:
    """One upload per id, each then readable (the reuse check reads it back)."""
    for file_id in ids:
        respx.get(f"{API}/library/files/{file_id}").mock(
            return_value=httpx.Response(
                200, json={"id": file_id, "filename": f"file-{file_id}.3mf", "notes": None}
            )
        )
    return respx.post(f"{API}/library/files").mock(
        side_effect=[
            httpx.Response(200, json={"id": file_id, "filename": f"file-{file_id}.3mf"})
            for file_id in ids
        ]
    )


def uploaded_name(route: respx.Route, index: int = -1) -> str:
    content = route.calls[index].request.content.decode("utf-8", errors="replace")
    found = re.search(r'filename="([^"]*)"', content)
    assert found is not None
    return found.group(1)


def file_into_project(client: TestClient, output_id: str) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/api/v1/outputs/{output_id}/project-file", json={"project_id": PROJECT}
    )
    return response


def remember_h2c_at(client: TestClient, nozzle: str) -> None:
    """What the print dialog last chose for the model: printer 1 (an H2C) and a nozzle."""
    assert (
        client.put(
            "/api/v1/print/models/demo/choices",
            json={"printer_id": 1, "nozzles": [{"size": nozzle}]},
        ).status_code
        == 200
    )


@respx.mock
def test_generate_with_a_project_uploads_once_into_its_folder(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    project_folder_routes()
    uploaded = uploads(41)

    first = file_into_project(client, output_id)
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["project_id"], body["folder_id"], body["library_file_id"]) == (
        PROJECT,
        FOLDER,
        41,
    )
    assert body["created"] is True
    assert body["bambuddy_url"] == f"{BASE}/projects/{PROJECT}"
    assert uploaded.calls.last.request.url.params["folder_id"] == str(FOLDER)

    # The same project chosen twice: the route is idempotent per (folder, target).
    again = file_into_project(client, output_id).json()
    assert (again["library_file_id"], again["created"]) == (41, False)
    assert uploaded.call_count == 1


@respx.mock
def test_the_file_is_named_after_the_template_and_the_changed_params(
    client: TestClient, model: str
) -> None:
    """``Demo`` is the template's name; ``width`` 12 is the one value off its default
    (``label`` is left at ``hi``)."""
    configure(client)
    output_id = make_output(client, model)
    project_folder_routes()
    uploaded = uploads(41)

    body = file_into_project(client, output_id).json()
    assert uploaded_name(uploaded) == "Demo — 12.3mf"
    assert body["filename"] == "file-41.3mf"


@respx.mock
def test_a_name_already_in_the_folder_is_made_unique(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    project_folder_routes(
        [
            {"id": 30, "folder_id": FOLDER, "filename": "Demo — 12.3mf"},
            {"id": 31, "folder_id": FOLDER, "filename": "Demo — 12 (2).3mf"},
        ]
    )
    uploaded = uploads(41)

    file_into_project(client, output_id)
    assert uploaded_name(uploaded) == "Demo — 12 (3).3mf"


@respx.mock
def test_a_later_print_on_the_same_printer_reuses_the_project_file(
    client: TestClient, model: str
) -> None:
    """One file in the project folder, plus its slice. The print chose spools, so its
    layout key names their colours; the Generate copy is in the model's own colours
    and is still the one the print uses, rather than a second copy beside it."""
    configure(client)
    output_id = make_output(client, model)
    remember_h2c_at(client, "0.2")
    project_folder_routes()
    uploaded = uploads(41, 42)
    run_routes()
    slice_routes()
    queue_route()

    filed = file_into_project(client, output_id).json()
    assert filed["library_file_id"] == 41

    ran = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(project_id=PROJECT)
    )
    assert ran.status_code == 200, ran.text
    assert (ran.json()["library_file_id"], ran.json()["folder_id"]) == (41, FOLDER)
    assert uploaded.call_count == 1


@respx.mock
def test_generate_lays_the_file_out_for_the_projects_last_print(
    client: TestClient, model: str
) -> None:
    """A print into a project remembers its printer and nozzle (in Postgres), and the
    next output filed into that project is laid out for them, so it too is reused."""
    configure(client)
    printed = make_output(client, model)
    project_folder_routes()
    uploaded = uploads(41, 42)
    run_routes()
    slice_routes()
    queue_route()
    first = client.post(
        f"/api/v1/print/outputs/{printed}/run", json=run_request(project_id=PROJECT)
    )
    assert first.status_code == 200, first.text

    fresh = make_output(client, model, name="Second")
    assert file_into_project(client, fresh).json()["library_file_id"] == 42
    detail = client.get(f"/api/v1/outputs/{fresh}").json()
    assert [copy["target_key"] for copy in detail["library_files"]] == ["Bambu Lab H2C@0.2"]

    again = client.post(f"/api/v1/print/outputs/{fresh}/run", json=run_request(project_id=PROJECT))
    assert again.json()["library_file_id"] == 42
    assert uploaded.call_count == 2


@respx.mock
def test_a_print_on_a_different_printer_adds_a_second_copy_named_for_it(
    client: TestClient, model: str
) -> None:
    """The project keeps the Generate copy; the second printer model gets its own,
    named after that model so the two are told apart in the folder."""
    configure(client)
    output_id = make_output(client, model)
    project_folder_routes().side_effect = [
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[{"id": 41, "folder_id": FOLDER, "filename": "Demo — 12.3mf"}]),
    ]
    uploaded = uploads(41, 42)
    run_routes()
    slice_routes()
    queue_route()

    file_into_project(client, output_id)  # no printer known: the fallback plate
    ran = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(project_id=PROJECT)
    )
    assert ran.json()["library_file_id"] == 42
    assert uploaded_name(uploaded) == "Demo — 12 (H2C).3mf"
    assert uploaded.calls.last.request.url.params["folder_id"] == str(FOLDER)


@respx.mock
def test_generate_with_no_project_uploads_nothing(client: TestClient, model: str) -> None:
    """Generate on its own is ScadBuddy's: nothing reaches Bambuddy until a send."""
    configure(client)
    anything = respx.route(host=httpx.URL(BASE).host).mock(return_value=httpx.Response(200))
    make_output(client, model)
    assert not anything.called


def test_filing_needs_a_project(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    response = client.post(f"/api/v1/outputs/{output_id}/project-file", json={})
    assert response.status_code == 422


def test_the_stem_spells_out_what_changed_and_nothing_a_print_file_name_refuses() -> None:
    defaults: dict[str, ParamValue | None] = {
        "name": "",
        "size": 10.0,
        "border": True,
        "a": 1,
        "b": 2,
        "c": 3,
    }
    assert project_stem("Name sign", {"name": "Reagan", "size": 10.0}, defaults) == (
        "Name sign — Reagan"
    )
    assert project_stem("Sign", {"size": 12.5, "border": False}, defaults) == (
        "Sign — 12.5, no border"
    )
    assert project_stem("Sign", {"a": 5, "b": 6, "c": 7, "name": "x"}, defaults) == (
        "Sign — x, 5, 6…"
    )
    assert project_stem("Sign", {"name": 'A/B: "C"?'}, defaults) == "Sign — A-B- -C--"
    assert project_stem("Sign", {}, defaults, name="Elan") == "Sign — Elan"
    assert project_stem("Sign", {}, defaults) == "Sign"


@respx.mock
def test_the_customize_pages_choice_is_remembered_as_the_last_project(
    client: TestClient,
) -> None:
    """The picker on the Customize page and the print dialog's show the same project:
    both default to ``last_project_id``, which a choice on either updates."""
    configure(client)
    respx.get(f"{API}/projects/").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/library/folders").mock(return_value=httpx.Response(200, json=[]))

    def last() -> Any:
        return client.get("/api/v1/print/projects").json()["last_project_id"]

    chosen = client.put("/api/v1/print/projects/last", json={"project_id": PROJECT})
    assert chosen.json() == {"project_id": PROJECT}
    assert last() == PROJECT
    cleared = client.put("/api/v1/print/projects/last", json={"project_id": None})
    assert cleared.json() == {"project_id": None}
    assert last() is None
