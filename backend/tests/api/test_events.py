"""Every mutation route publishes its event (#264, #266), asserted with a listener on
the app's own bus. Payloads carry ids only, so what is checked is kind and ids."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState, get_fonts, get_libraries
from scadbuddy.core.events import Event, InProcessEventBus
from scadbuddy.library.libraries import CatalogueLibrary, LibraryStore
from tests.api.conftest import PNG_BYTES, wait_for_job
from tests.api.test_fonts import FakeBackedService, FakeClient
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import run_request, run_routes
from tests.api.test_send import BASE, configure, make_output, upload_route
from tests.conftest import make_library_upstream

API = f"{BASE}/api/v1"
SOURCE = 'width = 10;\nlabel = "hi";\n'


@pytest.fixture
def events(app: FastAPI) -> list[Event]:
    bus = getattr(app.state, STATE_ATTR).events
    assert isinstance(bus, InProcessEventBus)
    seen: list[Event] = []
    bus.add_listener(seen.append)
    return seen


@pytest.fixture
def mine(client: TestClient, events: list[Event]) -> str:
    """A template created through the API, so it is committed like any other."""
    _ok(client.post("/api/v1/models", json={"name": "Widget", "source": SOURCE}), 201)
    events.clear()
    return "widget"


def published(events: list[Event], kind: str | None = None) -> list[dict[str, Any]]:
    """What was published, as kind plus ids, in order; the event's own id and time
    are left out so the assertions read as the payloads they are about."""
    return [
        event.model_dump(exclude={"id", "at"})
        for event in events
        if kind is None or event.kind == kind
    ]


def queue_item(status: str) -> dict[str, Any]:
    return {"id": 51, "printer_id": 1, "printer_name": "3DP-31B-598", "status": status}


def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


# ── models ───────────────────────────────────────────────────────────────────────


def test_creating_a_model_publishes_model_created(client: TestClient, events: list[Event]) -> None:
    _ok(client.post("/api/v1/models", json={"name": "Widget", "source": SOURCE}), 201)
    _ok(
        client.post(
            "/api/v1/models",
            content=SOURCE,
            headers={"content-type": "text/plain", "X-Model-Name": "Pasted"},
        ),
        201,
    )
    _ok(
        client.post(
            "/api/v1/models", files={"file": ("uploaded.scad", SOURCE.encode(), "text/plain")}
        ),
        201,
    )
    assert published(events, "model.created") == [
        {"kind": "model.created", "slug": "widget"},
        {"kind": "model.created", "slug": "pasted"},
        {"kind": "model.created", "slug": "uploaded"},
    ]


def test_a_refused_create_publishes_nothing(
    client: TestClient, model: str, events: list[Event]
) -> None:
    response = client.post("/api/v1/models", json={"name": "Demo", "source": SOURCE})
    assert response.status_code == 409
    assert published(events) == []


@respx.mock
@pytest.mark.usefixtures("fake_dns")
def test_importing_a_model_publishes_model_created(client: TestClient, events: list[Event]) -> None:
    url = "https://raw.githubusercontent.com/someone/models/main/Bin.scad"
    respx.get(url).mock(return_value=httpx.Response(200, text=SOURCE))
    _ok(client.post("/api/v1/models/import", json={"url": url}), 201)
    assert published(events, "model.created") == [{"kind": "model.created", "slug": "bin"}]


def test_editing_metadata_publishes_model_updated(
    client: TestClient, model: str, events: list[Event]
) -> None:
    _ok(client.patch(f"/api/v1/models/{model}", json={"description": "new"}))
    assert published(events, "model.updated") == [{"kind": "model.updated", "slug": model}]


def test_saving_source_publishes_source_changed_and_model_updated(
    client: TestClient, model: str, events: list[Event]
) -> None:
    _ok(client.put(f"/api/v1/models/{model}/source", json={"source": SOURCE + "// v2\n"}))
    assert published(events, "source.changed") == [{"kind": "source.changed", "slug": model}]
    assert published(events, "model.updated") == [{"kind": "model.updated", "slug": model}]


def test_thumbnail_and_readme_writes_publish_model_updated(
    client: TestClient, model: str, events: list[Event]
) -> None:
    png = {"file": ("thumb.png", PNG_BYTES, "image/png")}
    _ok(client.put(f"/api/v1/models/{model}/thumbnail", files=png))
    _ok(client.delete(f"/api/v1/models/{model}/thumbnail"))
    _ok(client.put(f"/api/v1/models/{model}/readme", json={"content": "# Demo\n"}))
    _ok(client.delete(f"/api/v1/models/{model}/readme"))
    assert published(events, "model.updated") == [{"kind": "model.updated", "slug": model}] * 4


def test_duplicating_and_deleting_publish_created_and_deleted(
    client: TestClient, mine: str, events: list[Event]
) -> None:
    _ok(client.post(f"/api/v1/models/{mine}/duplicate", json={"name": "Copy"}), 201)
    _ok(client.delete("/api/v1/models/copy"), 204)
    assert published(events, "model.created") == [{"kind": "model.created", "slug": "copy"}]
    assert published(events, "model.deleted") == [{"kind": "model.deleted", "slug": "copy"}]


@pytest.mark.requires_git
def test_every_commit_publishes_version_committed(
    client: TestClient, mine: str, events: list[Event]
) -> None:
    saved = _ok(client.put(f"/api/v1/models/{mine}/source", json={"source": SOURCE + "// v2\n"}))
    committed = published(events, "version.committed")
    assert committed == [{"kind": "version.committed", "slug": mine, "commit": saved["version"]}]


@pytest.mark.requires_git
def test_restoring_a_version_publishes_source_changed_and_its_commit(
    client: TestClient, mine: str, events: list[Event]
) -> None:
    first = _ok(client.get(f"/api/v1/models/{mine}"))["version"]
    _ok(client.put(f"/api/v1/models/{mine}/source", json={"source": SOURCE + "// v2\n"}))
    events.clear()

    restored = _ok(client.post(f"/api/v1/models/{mine}/versions/{first}/restore"))

    assert published(events, "source.changed") == [{"kind": "source.changed", "slug": mine}]
    assert published(events, "version.committed") == [
        {"kind": "version.committed", "slug": mine, "commit": restored["commit"]}
    ]


@pytest.mark.requires_git
def test_an_upstream_edit_is_announced_to_its_duplicates(
    client: TestClient, mine: str, events: list[Event]
) -> None:
    _ok(client.post(f"/api/v1/models/{mine}/duplicate", json={"name": "Copy"}), 201)
    events.clear()

    saved = _ok(
        client.put(
            f"/api/v1/models/{mine}/source",
            json={"source": SOURCE.replace("width = 10", "width = 11")},
        )
    )

    assert published(events, "upstream.available") == [
        {
            "kind": "upstream.available",
            "slug": "copy",
            "upstream": mine,
            "commit": saved["version"],
        }
    ]


@pytest.mark.requires_git
def test_merging_dismissing_and_detaching_publish_their_events(
    client: TestClient, mine: str, events: list[Event]
) -> None:
    _ok(client.post(f"/api/v1/models/{mine}/duplicate", json={"name": "Copy"}), 201)
    _ok(client.put(f"/api/v1/models/{mine}/source", json={"source": SOURCE + "// v2\n"}))
    events.clear()
    _ok(client.post("/api/v1/models/copy/upstream/dismiss"))
    assert published(events, "model.updated") == [{"kind": "model.updated", "slug": "copy"}]

    events.clear()
    _ok(client.post("/api/v1/models/copy/upstream/merge"))
    assert published(events, "source.changed") == [{"kind": "source.changed", "slug": "copy"}]
    assert published(events, "model.updated") == [{"kind": "model.updated", "slug": "copy"}]

    _ok(client.delete(f"/api/v1/models/{mine}", params={"force": "true"}), 204)
    events.clear()
    _ok(client.post("/api/v1/models/copy/upstream/detach"))
    assert published(events, "model.updated") == [{"kind": "model.updated", "slug": "copy"}]


@pytest.mark.requires_git
def test_pinning_and_unpinning_a_library_publish_library_changed(
    app: FastAPI, client: TestClient, mine: str, events: list[Event], tmp_path: Path
) -> None:
    url, _ = make_library_upstream(tmp_path, {"v1": "module marker() cube(1);\n"})
    state: AppState = getattr(app.state, STATE_ATTR)
    store = LibraryStore(
        state.paths,
        catalogue=(
            CatalogueLibrary(
                name="BOSL2", url=url, ref="v1", licence="BSD-2-Clause", homepage="https://x"
            ),
        ),
        protocols=("file",),
    )
    app.dependency_overrides[get_libraries] = lambda: store

    _ok(client.put(f"/api/v1/models/{mine}/libraries/BOSL2", json={}))
    _ok(client.delete(f"/api/v1/models/{mine}/libraries/BOSL2"))

    assert (
        published(events, "library.changed")
        == [{"kind": "library.changed", "slug": mine, "name": "BOSL2"}] * 2
    )


@pytest.mark.requires_git
def test_repinning_and_removing_checkouts_publish_their_events(
    app: FastAPI, client: TestClient, mine: str, events: list[Event], tmp_path: Path
) -> None:
    """#253: a re-pin changes the model as a pin does; removing checkouts changes no
    model (it is refused while one pins them), so it is `library.removed` alone. A
    refused removal and a refused re-pin publish nothing."""
    url, commits = make_library_upstream(
        tmp_path, {"v1": "module marker() cube(1);\n", "v2": "module marker() cube(2);\n"}
    )
    state: AppState = getattr(app.state, STATE_ATTR)
    store = LibraryStore(
        state.paths,
        catalogue=(
            CatalogueLibrary(
                name="BOSL2", url=url, ref="v1", licence="BSD-2-Clause", homepage="https://x"
            ),
        ),
        protocols=("file",),
    )
    app.dependency_overrides[get_libraries] = lambda: store
    _ok(client.put(f"/api/v1/models/{mine}/libraries/BOSL2", json={}))
    events.clear()

    _ok(client.patch(f"/api/v1/models/{mine}/libraries/BOSL2", json={"ref": "v2"}))
    assert client.delete("/api/v1/libraries/BOSL2").status_code == 409  # still pinned
    assert client.patch("/api/v1/models/widget/libraries/other", json={}).status_code == 404
    repinned = [
        event.model_dump(exclude={"id", "at"})
        for event in events
        if event.kind in ("library.changed", "model.updated", "library.removed")
    ]
    events.clear()
    _ok(
        client.delete("/api/v1/libraries/BOSL2", params={"commit": commits["v1"]}),
        204,
    )

    assert repinned == [
        {"kind": "library.changed", "slug": mine, "name": "BOSL2"},
        {"kind": "model.updated", "slug": mine},
    ]
    assert published(events) == [
        {"kind": "library.removed", "name": "BOSL2", "commits": [commits["v1"]]}
    ]


# ── renders and outputs ──────────────────────────────────────────────────────────


def test_a_render_publishes_each_job_state(
    client: TestClient, model: str, events: list[Event]
) -> None:
    job_id = _ok(
        client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}}), 202
    )["job_id"]
    wait_for_job(client, job_id)

    assert published(events) == [
        {"kind": "job.pending", "job_id": job_id, "slug": model},
        {"kind": "job.running", "job_id": job_id, "slug": model},
        {"kind": "job.done", "job_id": job_id, "slug": model},
    ]


def test_a_failed_render_publishes_job_failed(
    client: TestClient, model: str, events: list[Event]
) -> None:
    job_id = _ok(
        client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 999}}), 202
    )["job_id"]
    wait_for_job(client, job_id)
    assert published(events)[-1] == {"kind": "job.failed", "job_id": job_id, "slug": model}


def test_saving_and_deleting_an_output_publish_their_events(
    client: TestClient, model: str, events: list[Event]
) -> None:
    output_id = make_output(client, model)
    _ok(client.delete(f"/api/v1/outputs/{output_id}"), 204)
    assert published(events, "output.created") == [
        {"kind": "output.created", "output_id": output_id, "slug": model}
    ]
    assert published(events, "output.deleted") == [
        {"kind": "output.deleted", "output_id": output_id, "slug": model}
    ]


# ── printing ─────────────────────────────────────────────────────────────────────


@respx.mock
def test_a_print_publishes_progress_and_then_settled_once(
    client: TestClient, model: str, events: list[Event]
) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queue_route()
    item = respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(200, json=queue_item("pending"))
    )
    events.clear()

    _ok(client.post(f"/api/v1/print/outputs/{output_id}/run", json=run_request()))
    _ok(client.get(f"/api/v1/print/outputs/{output_id}/progress"))
    _ok(client.get(f"/api/v1/print/outputs/{output_id}/progress"))  # nothing new
    item.mock(return_value=httpx.Response(200, json=queue_item("completed")))
    _ok(client.get(f"/api/v1/print/outputs/{output_id}/progress"))
    _ok(client.get(f"/api/v1/print/outputs/{output_id}/progress"))

    ids = {"output_id": output_id, "slug": model}
    assert [e for e in published(events) if e["kind"].startswith("print.")] == [
        {"kind": "print.progress", **ids},  # the run
        {"kind": "print.progress", **ids},  # the first read
        {"kind": "print.progress", **ids},  # it finished
        {"kind": "print.settled", **ids},
    ]


# ── settings and fonts ───────────────────────────────────────────────────────────


def test_every_settings_write_publishes_settings_changed(
    client: TestClient, model: str, events: list[Event]
) -> None:
    _ok(client.put("/api/v1/settings", json={"public_url": "https://scad.example"}))
    _ok(
        client.put(
            "/api/v1/settings/print-options",
            json={"scope": "global", "options": {"use_ams": False}},
        )
    )
    _ok(client.put(f"/api/v1/print/models/{model}/choices", json={"printer_id": 2}))
    _ok(client.put("/api/v1/print/printers/1/bed-type", json={"bed_type": "Supertack Plate"}))

    assert [event["section"] for event in published(events, "settings.changed")] == [
        "connection",
        "print_options",
        "model_choices",
        "printer_bed_type",
    ]


def test_installing_a_font_publishes_font_installed(
    app: FastAPI, client: TestClient, data_dir: Path, events: list[Event]
) -> None:
    service = FakeBackedService(data_dir, client=FakeClient())
    app.dependency_overrides[get_fonts] = lambda: service
    _ok(client.post("/api/v1/fonts/install", json={"family": "Pacifico"}))
    assert published(events, "font.installed") == [{"kind": "font.installed", "family": "Pacifico"}]


def test_a_broken_bus_never_fails_the_request(
    app: FastAPI,
    client: TestClient,
    model: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = getattr(app.state, STATE_ATTR).events

    def broken(event: Event) -> None:
        raise RuntimeError("the bus is down")

    monkeypatch.setattr(bus, "publish", broken)
    with caplog.at_level(logging.ERROR):
        _ok(client.patch(f"/api/v1/models/{model}", json={"description": "still saved"}))
        _ok(client.put("/api/v1/settings", json={"public_url": "https://scad.example"}))
    assert _ok(client.get(f"/api/v1/models/{model}"))["description"] == "still saved"
    assert "could not publish an event" in caplog.text
