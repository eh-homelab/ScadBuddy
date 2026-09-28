"""Default-render previews (#179 follow-up): the catalogue thumbnail of a model with
no image of its own and no generated output.

The render itself is stubbed -- `render_preview` is the render pipeline up to its
plate image, which the render tests already cover -- so these pin when a preview is
made, kept, replaced and dropped, and that it never touches the model's history.

The app runs on Postgres (`requires_postgres`), where the previews are rows in
``model_previews`` (#454); without a database there are none.
"""

from __future__ import annotations

import asyncio
import itertools
import subprocess
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.paths import BUILTIN_PREFIX, DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.render.bambu3mf import PLATE_THUMBNAIL
from scadbuddy.render.previews import PreviewScheduler
from scadbuddy.render.runner import OpenSCADError
from scadbuddy.render.submit import RenderService
from tests.api.conftest import PNG_BYTES, job_file, wait_for_job

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

SLUG = "widget"
SOURCE = 'width = 10;\nlabel = "hi";\ncube(width);\n'
PREVIEW = PNG_BYTES + b"default render"
_WIDTHS = itertools.count(20)


class StubRender:
    """Stands in for `render_preview`: records each call and the source it read."""

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths
        self.calls: list[tuple[str, str]] = []
        self.started: list[str] = []
        self.fail: Exception | None = None
        self.delay = 0.0
        #: Cleared, a render waits here -- how a test holds one in flight.
        self.released = threading.Event()
        self.released.set()

    async def __call__(self, slug: str, timeout: float) -> bytes:
        # As the app's runners do, it applies the timeout the scheduler hands it.
        return await asyncio.wait_for(self._render(slug), timeout)

    async def _render(self, slug: str) -> bytes:
        self.started.append(slug)
        while not self.released.is_set():
            await asyncio.sleep(0.01)
        if self.delay:
            await asyncio.sleep(self.delay)
        source = self.paths.model_source(slug).read_text(encoding="utf-8")
        self.calls.append((slug, source))
        if self.fail is not None:
            raise self.fail
        return PREVIEW + source.encode()


@pytest.fixture
def settings(settings: Settings, pg_conninfo: str) -> Settings:
    return settings.model_copy(update={"preview_renders": True, "database_url": pg_conninfo})


@pytest.fixture
def state(app: FastAPI) -> AppState:
    found: AppState = getattr(app.state, STATE_ATTR)
    return found


@pytest.fixture
def stub(state: AppState, paths: DataPaths) -> StubRender:
    render = StubRender(paths)
    scheduler(state).runner = render
    scheduler(state).debounce = 0.0
    scheduler(state).interval = 0.0
    return render


@pytest.fixture
def client(app: FastAPI, stub: StubRender) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def scheduler(state: AppState) -> PreviewScheduler:
    assert state.previews is not None
    return state.previews


def settle(client: TestClient, state: AppState) -> None:
    """Wait until the scheduler has nothing due and nothing in flight."""
    client.portal.call(scheduler(state).idle)  # type: ignore[union-attr]


def _create(
    client: TestClient, source: str = SOURCE, **files: tuple[str, bytes, str]
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/models",
        files={"file": (f"{SLUG}.scad", source.encode(), "application/octet-stream"), **files},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _model(client: TestClient, slug: str = SLUG) -> dict[str, Any]:
    body: dict[str, Any] = client.get(f"/api/v1/models/{slug}").json()
    return body


def _generate(client: TestClient, paths: DataPaths, cover: bytes) -> str:
    job_id = client.post(
        f"/api/v1/models/{SLUG}/render", json={"params": {"width": next(_WIDTHS)}}
    ).json()["job_id"]
    wait_for_job(client, job_id)
    with zipfile.ZipFile(job_file(paths, job_id, "model.3mf"), "a") as archive:
        archive.writestr(PLATE_THUMBNAIL, cover)
    response = client.post(f"/api/v1/models/{SLUG}/outputs", json={"job_id": job_id})
    assert response.status_code == 201, response.text
    output_id: str = response.json()["id"]
    return output_id


# ── on create ─────────────────────────────────────────────────────────────────


def test_a_model_created_without_a_thumbnail_gets_its_default_render(
    client: TestClient, state: AppState, stub: StubRender
) -> None:
    _create(client)
    settle(client, state)

    assert stub.calls == [(SLUG, SOURCE)]
    model = _model(client)
    assert model["has_thumbnail"] is True
    assert model["thumbnail_source"] == "preview"
    assert model["thumbnail_output_id"] is None
    assert len(model["thumbnail_preview_id"]) == 16
    served = client.get(f"/api/v1/models/{SLUG}/thumbnail")
    assert served.status_code == 200
    assert served.content == PREVIEW + SOURCE.encode()


def test_the_upload_answers_while_the_render_is_still_running(
    client: TestClient, state: AppState, stub: StubRender
) -> None:
    stub.released.clear()

    _create(client)
    for _ in range(200):
        if stub.started:
            break
        threading.Event().wait(0.01)

    # Answered, with the render held in flight behind it.
    assert stub.started == [SLUG]
    assert _model(client)["thumbnail_source"] is None
    stub.released.set()
    settle(client, state)
    assert _model(client)["thumbnail_source"] == "preview"


def test_a_model_created_with_a_thumbnail_is_never_rendered(
    client: TestClient, state: AppState, stub: StubRender
) -> None:
    _create(client, thumbnail=("t.png", PNG_BYTES, "image/png"))
    settle(client, state)

    assert stub.calls == []
    assert _model(client)["thumbnail_source"] == "model"


def test_a_duplicate_gets_a_preview_of_its_own(
    client: TestClient, state: AppState, stub: StubRender, paths: DataPaths
) -> None:
    _create(client)
    copy = client.post(f"/api/v1/models/{SLUG}/duplicate", json={"name": "Copy"})
    assert copy.status_code == 201, copy.text
    settle(client, state)

    assert sorted(slug for slug, _ in stub.calls) == ["copy", SLUG]
    assert _model(client, "copy")["thumbnail_source"] == "preview"
    assert scheduler(state).store.image("copy") is not None


def test_a_model_can_be_deleted_while_its_preview_is_pending(
    client: TestClient, state: AppState, stub: StubRender, paths: DataPaths
) -> None:
    """A preview is not a render job, so it never makes a delete wait (409)."""
    stub.released.clear()
    _create(client)

    assert client.delete(f"/api/v1/models/{SLUG}").status_code == 204
    stub.released.set()
    settle(client, state)
    assert scheduler(state).store.record(SLUG) is None


# ── precedence ────────────────────────────────────────────────────────────────


def test_its_own_thumbnail_replaces_the_preview_and_removing_it_brings_one_back(
    client: TestClient, state: AppState, stub: StubRender, paths: DataPaths
) -> None:
    _create(client)
    settle(client, state)
    assert scheduler(state).store.image(SLUG) is not None

    own = client.put(
        f"/api/v1/models/{SLUG}/thumbnail", files={"file": ("t.png", PNG_BYTES, "image/png")}
    )
    assert own.json()["thumbnail_source"] == "model"
    # Dropped with the write, not later: there is nothing left for it to stand in for.
    assert scheduler(state).store.record(SLUG) is None
    settle(client, state)
    assert len(stub.calls) == 1

    assert client.delete(f"/api/v1/models/{SLUG}/thumbnail").status_code == 200
    settle(client, state)
    assert len(stub.calls) == 2
    assert _model(client)["thumbnail_source"] == "preview"


def test_a_generated_output_outranks_the_preview_until_it_is_deleted(
    client: TestClient, state: AppState, stub: StubRender, paths: DataPaths
) -> None:
    _create(client)
    settle(client, state)

    output_id = _generate(client, paths, PNG_BYTES + b"plate")
    settle(client, state)
    model = _model(client)
    assert (model["thumbnail_source"], model["thumbnail_output_id"]) == ("output", output_id)
    assert model["thumbnail_preview_id"] is None
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == PNG_BYTES + b"plate"
    # Nothing left for it to stand in for.
    assert scheduler(state).store.record(SLUG) is None

    assert client.delete(f"/api/v1/outputs/{output_id}").status_code == 204
    settle(client, state)
    assert _model(client)["thumbnail_source"] == "preview"


@pytest.mark.requires_postgres
def test_the_preview_is_never_listed_as_an_output(
    client: TestClient, state: AppState, stub: StubRender
) -> None:
    _create(client)
    settle(client, state)

    assert client.get(f"/api/v1/models/{SLUG}/outputs").json() == []


# ── invalidation ──────────────────────────────────────────────────────────────


def test_a_source_edit_re_renders_and_changes_the_preview_id(
    client: TestClient, state: AppState, stub: StubRender
) -> None:
    _create(client)
    settle(client, state)
    before = _model(client)["thumbnail_preview_id"]

    edited = SOURCE.replace("width = 10", "width = 12")
    assert (
        client.put(
            f"/api/v1/models/{SLUG}/source", json={"source": edited, "force": True}
        ).status_code
        == 200
    )
    settle(client, state)

    assert stub.calls[-1] == (SLUG, edited)
    after = _model(client)
    assert after["thumbnail_preview_id"] != before
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == PREVIEW + edited.encode()


def test_a_restored_revision_is_rendered_again(
    client: TestClient, state: AppState, stub: StubRender
) -> None:
    _create(client)
    settle(client, state)
    first = client.get(f"/api/v1/models/{SLUG}/versions").json()[0]["commit"]
    client.put(
        f"/api/v1/models/{SLUG}/source",
        json={"source": SOURCE.replace("10", "12"), "force": True},
    )
    settle(client, state)

    restored = client.post(f"/api/v1/models/{SLUG}/versions/{first}/restore")
    assert restored.status_code == 200, restored.text
    settle(client, state)

    assert [source for _, source in stub.calls] == [SOURCE, SOURCE.replace("10", "12"), SOURCE]


def test_a_readme_or_metadata_edit_does_not_re_render(
    client: TestClient, state: AppState, stub: StubRender
) -> None:
    _create(client)
    settle(client, state)

    client.put(f"/api/v1/models/{SLUG}/readme", json={"content": "# Widget\n"})
    client.patch(f"/api/v1/models/{SLUG}", json={"name": "Widget Two"})
    settle(client, state)

    assert len(stub.calls) == 1


def test_a_burst_of_changes_renders_once(
    client: TestClient, state: AppState, stub: StubRender
) -> None:
    scheduler(state).debounce = 0.3
    _create(client)
    for tags in ("a", "b", "c"):
        client.patch(f"/api/v1/models/{SLUG}", json={"tags": [tags]})
    settle(client, state)

    assert stub.calls == [(SLUG, SOURCE)]


# ── failure ───────────────────────────────────────────────────────────────────


def test_a_failed_render_leaves_no_preview_and_is_not_retried_for_the_same_source(
    client: TestClient,
    state: AppState,
    stub: StubRender,
    paths: DataPaths,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stub.fail = OpenSCADError("openscad exited with 1", ["ERROR: boom"])
    _create(client)
    settle(client, state)

    assert _model(client)["thumbnail_source"] is None
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").status_code == 404
    assert scheduler(state).store.image(SLUG) is None
    assert "the default render for a preview failed" in caplog.text

    # Asked again with nothing changed: no second attempt.
    scheduler(state).request(SLUG)
    client.patch(f"/api/v1/models/{SLUG}", json={"name": "Still Broken"})
    settle(client, state)
    assert len(stub.calls) == 1

    # A new source is a new attempt.
    stub.fail = None
    client.put(
        f"/api/v1/models/{SLUG}/source",
        json={"source": SOURCE.replace("10", "11"), "force": True},
    )
    settle(client, state)
    assert len(stub.calls) == 2
    assert _model(client)["thumbnail_source"] == "preview"


def test_a_render_that_times_out_is_a_failure(
    client: TestClient, state: AppState, stub: StubRender, paths: DataPaths
) -> None:
    scheduler(state).timeout = 0.05
    stub.delay = 1.0
    _create(client)
    settle(client, state)

    assert _model(client)["thumbnail_source"] is None
    record = scheduler(state).store.record(SLUG)
    assert record is not None
    assert (record.ok, record.error) == (False, "TimeoutError")


# ── history ───────────────────────────────────────────────────────────────────


def test_a_preview_is_never_committed_or_left_in_the_model_directory(
    client: TestClient, state: AppState, stub: StubRender, paths: DataPaths
) -> None:
    _create(client)
    versions = client.get(f"/api/v1/models/{SLUG}/versions").json()
    settle(client, state)

    assert scheduler(state).store.image(SLUG) is not None
    assert client.get(f"/api/v1/models/{SLUG}/versions").json() == versions
    assert sorted(entry.name for entry in paths.model_dir(SLUG).iterdir()) == [
        "model.json",
        "model.scad",
    ]
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=paths.models,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""


# ── built-ins and the boot-time pass ─────────────────────────────────────────


def _seed(seed_dir: Path, slug: str, *, thumbnail: bool) -> None:
    directory = seed_dir / slug
    directory.mkdir()
    (directory / "model.scad").write_text(SOURCE, encoding="utf-8")
    (directory / "model.json").write_text(f'{{"name": "{slug}"}}\n', encoding="utf-8")
    if thumbnail:
        (directory / "thumbnail.png").write_bytes(PNG_BYTES)


def _boot(settings: Settings, stub: StubRender) -> tuple[FastAPI, AppState]:
    """A fresh process on the same data volume, with the stub render."""
    app = create_app(settings)
    booted: AppState = getattr(app.state, STATE_ATTR)
    if booted.previews is not None:
        booted.previews.runner = stub
        booted.previews.debounce = 0.0
        booted.previews.interval = 0.0
    return app, booted


def test_boot_renders_each_model_without_a_thumbnail_once(
    settings: Settings, seed_dir: Path, paths: DataPaths
) -> None:
    _seed(seed_dir, "bare", thumbnail=False)
    _seed(seed_dir, "pictured", thumbnail=True)
    paths.model_dir("mine").mkdir(parents=True)
    paths.model_source("mine").write_text(SOURCE, encoding="utf-8")
    stub = StubRender(paths)

    app, booted = _boot(settings, stub)
    with TestClient(app) as client:
        settle(client, booted)
        bare = _model(client, f"{BUILTIN_PREFIX}bare")
        pictured = _model(client, f"{BUILTIN_PREFIX}pictured")
        mine = _model(client, "mine")

    assert sorted(slug for slug, _ in stub.calls) == ["builtin:bare", "mine"]
    assert (bare["origin"], bare["thumbnail_source"]) == ("builtin", "preview")
    assert pictured["thumbnail_source"] == "model"
    assert mine["thumbnail_source"] == "preview"

    # A second boot finds every preview current and renders nothing.
    app, booted = _boot(settings, stub)
    with TestClient(app) as client:
        settle(client, booted)
    assert len(stub.calls) == 2


def test_previews_can_be_turned_off(settings: Settings, paths: DataPaths) -> None:
    stub = StubRender(paths)
    app, _ = _boot(settings.model_copy(update={"preview_renders": False}), stub)
    with TestClient(app) as client:
        _create(client)
        assert _model(client)["thumbnail_source"] is None
    assert stub.calls == []


def test_turning_previews_off_hides_the_ones_already_rendered(
    settings: Settings, paths: DataPaths
) -> None:
    """Off means no preview is served, not merely that none is made: one rendered
    while previews were on stays stored, but the catalogue no longer reads it."""
    stub = StubRender(paths)
    app, booted = _boot(settings, stub)
    with TestClient(app) as client:
        _create(client)
        settle(client, booted)
        assert _model(client)["thumbnail_source"] == "preview"

    app, off = _boot(settings.model_copy(update={"preview_renders": False}), stub)
    with TestClient(app) as client:
        assert off.previews is None
        assert off.catalogue.previews is not None
        assert off.catalogue.previews.image(SLUG) is not None
        model = _model(client)
        listed = client.get("/api/v1/models").json()
        served = client.get(f"/api/v1/models/{SLUG}/thumbnail")

    assert (model["has_thumbnail"], model["thumbnail_source"]) == (False, None)
    assert model["thumbnail_preview_id"] is None
    assert [entry["thumbnail_source"] for entry in listed if entry["slug"] == SLUG] == [None]
    assert served.status_code == 404
    assert len(stub.calls) == 1


# ── startup: the backfill never leaks the queue ───────────────────────────────


def _failing_backfill(
    booted: AppState, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> list[str]:
    """`list_models` fails: its first call at boot is the preview backfill's, after
    the queue has opened. Returns the log of what was closed."""

    def listing() -> Any:
        raise error

    monkeypatch.setattr(booted.catalogue, "list_models", listing)
    closed: list[str] = []
    for name, part in (("previews", scheduler(booted)), ("queue", booted.queue)):
        aclose = part.aclose

        async def recording(name: str = name, aclose: Any = aclose) -> None:
            closed.append(name)
            await aclose()

        monkeypatch.setattr(part, "aclose", recording)
    return closed


def test_a_failing_backfill_at_startup_still_closes_the_queue_and_previews(
    settings: Settings, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything the backfill raises past the render service starting is cleaned up as
    a shutdown is: its reconciler and the projection's pool are released."""
    app, booted = _boot(settings, StubRender(paths))
    closed = _failing_backfill(booted, monkeypatch, RuntimeError("listing blew up"))

    with pytest.raises(RuntimeError, match="listing blew up"), TestClient(app):
        pass

    assert closed == ["previews", "queue"]
    assert isinstance(booted.queue, RenderService)
    assert booted.queue._reconciler is None
    assert booted.projection is not None and booted.projection.pool.closed


def test_a_listing_error_costs_the_backfill_not_the_boot(
    settings: Settings,
    paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app, booted = _boot(settings, StubRender(paths))
    closed = _failing_backfill(booted, monkeypatch, OSError("volume went away"))

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    # `_boot` configures logging after `caplog` would have hooked in; the app logs
    # JSON to stdout, which is captured here.
    assert "could not list the models to render their previews" in capsys.readouterr().out
    assert closed == ["previews", "queue"]


def test_a_render_that_failed_on_the_worker_is_recorded_like_one_that_failed_here(
    client: TestClient, state: AppState, stub: StubRender
) -> None:
    stub.fail = WorkflowFailureError(
        cause=ApplicationError("openscad exited with 1", type="OpenSCADError")
    )
    _create(client)
    settle(client, state)

    record = scheduler(state).store.record(SLUG)
    assert record is not None and not record.ok


def test_an_infrastructure_error_is_not_recorded_and_the_next_pass_tries_again(
    client: TestClient, state: AppState, stub: StubRender
) -> None:
    stub.fail = RPCError("no worker is polling", RPCStatusCode.UNAVAILABLE, b"")
    _create(client)
    settle(client, state)
    assert scheduler(state).store.record(SLUG) is None

    stub.fail = None
    scheduler(state).request(SLUG)
    settle(client, state)
    assert len(stub.calls) == 2
    assert _model(client)["thumbnail_source"] == "preview"
