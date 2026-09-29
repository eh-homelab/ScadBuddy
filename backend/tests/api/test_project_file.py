"""Issue #317 — Generate with a project files the editable 3MF in its Bambuddy folder.

``POST /outputs/{id}/project-file`` is what the Customize page calls once Generate has
saved an output with a project chosen. It uploads through the same library-copy cache a
print uses (#316), so a later print on the same printer and nozzle reuses that one file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api import outputs as outputs_api
from scadbuddy.bambuddy.project_file import STEM_MAX_UTF16_UNITS, project_stem
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.outputs import META_NAME
from scadbuddy.render.schema import ParamValue
from tests.api.conftest import set_fake_env
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import _uploaded_colours, run_print, run_request, run_routes
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


def named_output(client: TestClient, model: str, name: str = "Elan") -> str:
    """An output whose model's schema is cached, as every real render of it leaves it
    (the test render is faked). The file name reads the defaults from that cache."""
    output_id = make_output(client, model, name=name)
    assert client.get(f"/api/v1/models/{model}/schema").status_code == 200
    return output_id


def in_spool_nines_colour(paths: DataPaths, model: str, output_id: str) -> None:
    """Make the output's own colour spool 9's (#688197, inventory-spools.json), so a
    print from spool 9 is in the colours Generate filed the project file in."""
    meta_path = paths.output_dir(model, output_id) / META_NAME
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["colors"] = ["#688197"]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


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
    output_id = named_output(client, model)
    project_folder_routes()
    uploaded = uploads(41)

    body = file_into_project(client, output_id).json()
    assert uploaded_name(uploaded) == "Demo — 12.3mf"
    assert body["filename"] == "file-41.3mf"


@respx.mock
def test_a_name_already_in_the_folder_is_made_unique(client: TestClient, model: str) -> None:
    configure(client)
    output_id = named_output(client, model)
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
def test_a_later_print_on_the_same_printer_in_the_models_colours_reuses_the_project_file(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """One file in the project folder, plus its slice. The print chose spools, so its
    layout key names their colours; they are the model's own, so the Generate copy is
    the one the print uses, rather than a second copy beside it."""
    configure(client)
    output_id = make_output(client, model)
    in_spool_nines_colour(paths, model, output_id)
    remember_h2c_at(client, "0.2")
    project_folder_routes()
    uploaded = uploads(41, 42)
    run_routes()
    slice_routes()
    queue_route()

    filed = file_into_project(client, output_id).json()
    assert filed["library_file_id"] == 41

    ran = run_print(client, output_id, json=run_request(project_id=PROJECT))
    assert ran.status_code == 200, ran.text
    assert (ran.json()["library_file_id"], ran.json()["folder_id"]) == (41, FOLDER)
    assert uploaded.call_count == 1


@respx.mock
def test_a_print_into_the_project_in_other_spools_colours_uploads_a_copy_in_theirs(
    client: TestClient, model: str
) -> None:
    """#476 in a project's folder: the Generate copy is in the model's colours (#FF0000),
    and spool 9 is #688197. Reusing it would show Bambuddy's queue a print that will
    not come out, so the print gets its own copy beside it, in the spool's colour."""
    configure(client)
    output_id = make_output(client, model)
    remember_h2c_at(client, "0.2")
    project_folder_routes()
    uploaded = uploads(41, 42)
    run_routes()
    slice_routes()
    queue_route()

    assert file_into_project(client, output_id).json()["library_file_id"] == 41
    assert _uploaded_colours(uploaded) == ["#FF0000"]

    ran = run_print(client, output_id, json=run_request(project_id=PROJECT))
    assert ran.status_code == 200, ran.text
    assert (ran.json()["library_file_id"], ran.json()["folder_id"]) == (42, FOLDER)
    assert uploaded.calls.last.request.url.params["folder_id"] == str(FOLDER)
    assert _uploaded_colours(uploaded) == ["#688197"]


@respx.mock
def test_naming_the_file_never_fails_the_print(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever resolving the name raises (a library checkout that cannot be cloned
    back, say), the print goes ahead under a plain name."""
    configure(client)
    output_id = make_output(client, model)
    project_folder_routes()
    uploaded = uploads(41)
    run_routes()
    slice_routes()
    queue_route()

    def broken(*args: object) -> str:
        raise RuntimeError("the library checkout is gone")

    monkeypatch.setattr(outputs_api, "_output_stem", broken)
    ran = run_print(client, output_id, json=run_request(project_id=PROJECT))
    assert ran.status_code == 200, ran.text
    assert uploaded_name(uploaded) == f"{project_stem(model, {}, {}, name='Elan')}.3mf"


@respx.mock
def test_a_folder_listing_that_fails_never_fails_the_print(client: TestClient, model: str) -> None:
    """The listing only makes the name unique: a 5xx there names the copy plainly and
    the print goes ahead (#540 review)."""
    configure(client)
    output_id = named_output(client, model)
    project_folder_routes().mock(return_value=httpx.Response(503, json={"detail": "busy"}))
    uploaded = uploads(41)
    run_routes()
    slice_routes()
    queue_route()

    ran = run_print(client, output_id, json=run_request(project_id=PROJECT))
    assert ran.status_code == 200, ran.text
    assert uploaded_name(uploaded) == "Demo — 12.3mf"


@respx.mock
def test_filing_after_a_print_in_the_models_colours_reuses_the_prints_copy(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The print's copy is recorded under its spools' colours; they are the model's own,
    so Generate's filing (no spools) reuses it rather than uploading a second one."""
    configure(client)
    output_id = make_output(client, model)
    in_spool_nines_colour(paths, model, output_id)
    remember_h2c_at(client, "0.2")
    project_folder_routes()
    uploaded = uploads(41, 42)
    run_routes()
    slice_routes()
    queue_route()

    ran = run_print(client, output_id, json=run_request(project_id=PROJECT))
    assert ran.status_code == 200, ran.text
    assert ran.json()["library_file_id"] == 41

    filed = file_into_project(client, output_id).json()
    assert (filed["library_file_id"], filed["created"]) == (41, False)
    assert uploaded.call_count == 1


def rewrite_cached_defaults(paths: DataPaths, model: str, **entry: Any) -> None:
    """Cached defaults under which ``width`` (12, against 5) is the one changed value, so
    a name read from them is ``Demo — 12``; ``entry`` overrides the cache entry's keys."""
    cache = paths.model_schema_cache(model)
    body = json.loads(cache.read_text(encoding="utf-8"))
    initials = {"width": 5}
    for param in body["schema"]["parameters"]:
        param["initial"] = initials.get(param["name"], param["initial"])
    body.update(entry)
    cache.write_text(json.dumps(body), encoding="utf-8")


@respx.mock
def test_the_name_reads_the_defaults_from_a_valid_cached_schema(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client)
    output_id = named_output(client, model)
    rewrite_cached_defaults(paths, model)
    project_folder_routes()
    uploaded = uploads(41)

    file_into_project(client, output_id)
    assert uploaded_name(uploaded) == "Demo — 12.3mf"


@pytest.mark.parametrize(
    "stale",
    [
        pytest.param({"version": -1}, id="cache-version"),
        pytest.param({"format": -1}, id="schema-format"),
        pytest.param({"library_path": ["/elsewhere/lib"]}, id="library-pins"),
    ],
)
@respx.mock
def test_a_cached_schema_the_renderer_would_not_use_does_not_name_the_file(
    client: TestClient, model: str, paths: DataPaths, stale: dict[str, Any]
) -> None:
    """The same validity as ``load_cached_schema`` (#540 review): an entry for another
    cache version, schema format or set of library pins is not this model's schema,
    so its defaults are not read, and the name falls back to the output's own."""
    configure(client)
    output_id = named_output(client, model)
    rewrite_cached_defaults(paths, model, **stale)
    project_folder_routes()
    uploaded = uploads(41)

    file_into_project(client, output_id)
    assert uploaded_name(uploaded) == "Demo — Elan.3mf"


@respx.mock
def test_the_name_runs_no_openscad_when_no_schema_is_cached(
    client: TestClient, model: str, tmp_path: Path
) -> None:
    """Without the cached schema every param counts as changed; the name is still the
    template's, and nothing is derived inline to get it."""
    configure(client)
    output_id = make_output(client, model)
    project_folder_routes()
    uploaded = uploads(41)
    log = tmp_path / "invocations.log"
    set_fake_env(tmp_path, "FAKE_OPENSCAD_LOG", str(log))

    assert file_into_project(client, output_id).status_code == 200
    assert uploaded_name(uploaded) == "Demo — 12.3mf"
    assert not log.exists()


@respx.mock
def test_generate_lays_the_file_out_for_the_projects_last_print(
    client: TestClient, model: str, paths: DataPaths
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
    first = run_print(client, printed, json=run_request(project_id=PROJECT))
    assert first.status_code == 200, first.text

    fresh = make_output(client, model, name="Second")
    in_spool_nines_colour(paths, model, fresh)
    assert file_into_project(client, fresh).json()["library_file_id"] == 42
    detail = client.get(f"/api/v1/outputs/{fresh}").json()
    assert [copy["target_key"] for copy in detail["library_files"]] == ["Bambu Lab H2C@0.2"]

    again = run_print(client, fresh, json=run_request(project_id=PROJECT))
    assert again.json()["library_file_id"] == 42
    assert uploaded.call_count == 2


@respx.mock
def test_a_print_on_a_different_printer_adds_a_second_copy_named_for_it(
    client: TestClient, model: str
) -> None:
    """The project keeps the Generate copy; the second printer model gets its own,
    named after that model so the two are told apart in the folder."""
    configure(client)
    output_id = named_output(client, model)
    project_folder_routes().side_effect = [
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[{"id": 41, "folder_id": FOLDER, "filename": "Demo — 12.3mf"}]),
    ]
    uploaded = uploads(41, 42)
    run_routes()
    slice_routes()
    queue_route()

    file_into_project(client, output_id)  # no printer known: the fallback plate
    ran = run_print(client, output_id, json=run_request(project_id=PROJECT))
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


def test_the_stem_is_capped_in_utf16_code_units_as_fat32_and_exfat_count_a_name() -> None:
    """A name on the printer's SD card is at most 255 UTF-16 code units; an emoji takes
    two. The stem keeps to its budget in those units, never splitting a pair, so the
    `` (H2D)`` or `` (12)`` suffix and ``.3mf`` still fit."""
    ascii_stem = project_stem("x" * 300, {}, {})
    assert len(ascii_stem) == STEM_MAX_UTF16_UNITS

    emoji_stem = project_stem("\N{SMILING FACE WITH SMILING EYES}" * 300, {}, {})
    assert len(emoji_stem.encode("utf-16-le")) // 2 == STEM_MAX_UTF16_UNITS
    assert emoji_stem == "\N{SMILING FACE WITH SMILING EYES}" * (STEM_MAX_UTF16_UNITS // 2)

    odd = project_stem("a" + "\N{SMILING FACE WITH SMILING EYES}" * 300, {}, {})
    assert len(odd.encode("utf-16-le")) // 2 == STEM_MAX_UTF16_UNITS - 1


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


def remember_project(client: TestClient, project_id: int | None) -> None:
    assert (
        client.put("/api/v1/print/projects/last", json={"project_id": project_id}).status_code
        == 200
    )


@respx.mock
def test_an_explicit_no_project_on_the_run_wins_over_the_remembered_one(
    client: TestClient, model: str
) -> None:
    """The picker's "No project" is sent as ``project_id: null``. The PUT that remembers
    it is fire-and-forget, so the run can arrive while ``last_project_id`` still names
    the old project; the request's own choice is the one honoured (#540 review)."""
    configure(client)
    output_id = make_output(client, model)
    remember_project(client, PROJECT)
    folder = respx.get(f"{API}/library/folders/by-project/{PROJECT}").mock(
        return_value=httpx.Response(
            200, json=[{"id": FOLDER, "name": "Kids' room", "project_id": PROJECT}]
        )
    )
    uploaded = uploads(41)
    run_routes()
    slice_routes()
    queue_route()

    ran = run_print(client, output_id, json={**run_request(), "project_id": None})

    assert ran.status_code == 200, ran.text
    assert ran.json()["project_id"] is None
    assert uploaded.calls.last.request.url.params["folder_id"] == "2"
    assert not folder.called


@respx.mock
def test_a_run_that_names_no_project_uses_the_remembered_one(
    client: TestClient, model: str
) -> None:
    """Omitting ``project_id`` (an agent that has no opinion) still means the last one."""
    configure(client)
    output_id = make_output(client, model)
    remember_project(client, PROJECT)
    project_folder_routes()
    uploaded = uploads(41)
    run_routes()
    slice_routes()
    queue_route()

    ran = run_print(client, output_id, json=run_request())

    assert ran.status_code == 200, ran.text
    assert (ran.json()["project_id"], ran.json()["folder_id"]) == (PROJECT, FOLDER)
    assert uploaded.calls.last.request.url.params["folder_id"] == str(FOLDER)


def test_attaching_with_an_explicit_no_project_files_nothing(
    client: TestClient, model: str
) -> None:
    """As on the run: ``null`` is "No project", not "the remembered one"."""
    configure(client)
    output_id = make_output(client, model)
    remember_project(client, PROJECT)

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/project",
        json={"project_id": None, "queue_item_ids": [71]},
    )

    assert response.status_code == 409, response.text
