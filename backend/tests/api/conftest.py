from __future__ import annotations

import json
import os
import time
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import psycopg
import pytest
import trimesh
from fastapi import FastAPI
from fastapi.testclient import TestClient
from temporalio.client import Client

from scadbuddy.activity.component import ACTIVITY
from scadbuddy.activity.index import ActivityIndex, Attribution
from scadbuddy.activity.models import ActivityRow, ActivityStatus
from scadbuddy.api import printing as printing_api
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.settings import Settings
from scadbuddy.flows.agent_tools import AgentTools, AgentToolsUnavailableError
from scadbuddy.main import create_app
from scadbuddy.render import submit as submit_module
from scadbuddy.render.bambu3mf import PLATE_THUMBNAIL
from scadbuddy.workflows.client import DEPLOYMENT_NAME, LIBRARY_DEPLOYMENT_NAME
from scadbuddy.workflows.commands import COMMAND_ANSWER_DEADLINE
from scadbuddy.workflows.housekeeping import prune_schedule_id_for, schedule_id_for
from scadbuddy.workflows.previews import preview_schedule_id_for
from tests.conftest import TEST_ANSWER_DEADLINE, write_openscad_3mf
from tests.support.deployment import (
    BUILD_SERVES_MAX_POLL,
    BUILD_SERVES_POLL,
    BUILD_SERVES_TIMEOUT,
    wait_until_build_serves,
)
from tests.support.store import content, pool, s3  # noqa: F401
from tests.support.temporal import (
    WorkflowReaper,
    temporal_available,
    temporal_server,
)

FAIL_WIDTH = 999.0
#: What the failing stub says it could not open (#408).
FAILED_WARNING = "OpenSCAD could not open pic.svg"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake png body"


def set_fake_env(directory: Path, name: str, value: Any) -> None:
    """Hand the fake binaries in ``directory`` a setting.

    Not an environment variable: the backend gives openscad and openscad-lsp an
    allowlisted environment (#281), so a ``monkeypatch.setenv`` never reaches them.
    """
    target = directory / "fake-env.json"
    current = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    target.write_text(json.dumps({**current, name: value}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray SCADBUDDY_* on the developer's machine must not reach the app."""
    for key in list(os.environ):
        if key.startswith("SCADBUDDY_"):
            monkeypatch.delenv(key)


@pytest.fixture(scope="session")
def temporal_address() -> Iterator[str]:
    """One Temporal for the whole session (a dev server, unless
    SCADBUDDY_TEST_TEMPORAL_ADDRESS names one): every API test renders on it."""
    if not temporal_available():
        pytest.skip("no Temporal: set SCADBUDDY_TEST_TEMPORAL_ADDRESS or put `temporal` on PATH")
    with temporal_server() as address:
        yield address


@pytest.fixture(scope="session")
def workflow_reaper(temporal_address: str) -> Iterator[WorkflowReaper]:
    with WorkflowReaper(temporal_address, "default") as reaper:
        yield reaper


@pytest.fixture(autouse=True)
def _temporal(temporal_address: str) -> None:
    """The API tests skip without a Temporal: the app renders nowhere else (#546)."""


@pytest.fixture(autouse=True)
def _print_answer_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The print route's own answer deadline (`COMMAND_ANSWER_DEADLINE`, and
    `ACCEPT_BUDGET` built on it) waits as long as `start_command`'s test default
    (`tests.conftest.TEST_ANSWER_DEADLINE`): under load its first start answered a 503
    `command-still-accepting`. Only the API tests reach that route (review #1316 4a)."""
    longer = (TEST_ANSWER_DEADLINE - COMMAND_ANSWER_DEADLINE).total_seconds()
    monkeypatch.setattr(printing_api, "COMMAND_ANSWER_DEADLINE", TEST_ANSWER_DEADLINE)
    monkeypatch.setattr(printing_api, "ACCEPT_BUDGET", printing_api.ACCEPT_BUDGET + longer)
    # Under load, a print run's execution outlived the 5 s a reprint waits for it to
    # close, so the reprint joined it and answered the earlier run.
    monkeypatch.setattr(printing_api, "CLOSING_WAIT", printing_api.CLOSING_WAIT + longer)
    # The render submit's bound is built on the same deadline at import: under load a
    # render's first submit answered a 503 `command-still-accepting` past it, and the
    # tests that render an output to send or print failed on it.
    monkeypatch.setattr(submit_module, "SUBMIT_DEADLINE", submit_module.SUBMIT_DEADLINE + longer)


@pytest.fixture
def settings(
    settings: Settings,
    temporal_address: str,
    workflow_reaper: WorkflowReaper,
    fake_openscad: str,
) -> Iterator[Settings]:
    """The app renders on the session's Temporal with its own in-process worker, on a
    task queue of its own: each test has its own data directory and database schema,
    so a worker is per app, not per session. The fake openscad exports a red 10x10x5
    box, and fails a render with ``width: 999`` (FAIL_WIDTH).

    Afterwards every workflow still open on the queue is terminated: a piece is
    abandoned by the job that started it, and one left running (its worker gone) would
    be joined by the next test to render the same piece, which would wait on it."""
    directory = Path(fake_openscad).parent
    drawn = write_openscad_3mf(
        directory / "drawn.3mf",
        [("Color 1", "#FF000000", trimesh.creation.box(extents=(10, 10, 5)))],
    )
    set_fake_env(directory, "FAKE_3MF", str(drawn))
    queue = f"api-{uuid.uuid4().hex[:12]}"
    yield settings.model_copy(
        update={
            "temporal_address": temporal_address,
            "temporal_namespace": "default",
            "temporal_task_queue_render": queue,
            "temporal_task_queue_bambuddy": f"{queue}-bambuddy",
            "temporal_task_queue_library": f"{queue}-library",
            "temporal_task_queue_projects": f"{queue}-projects",
            "temporal_task_queue_slicer": f"{queue}-slicer",
            "temporal_worker_inprocess": True,
            # No sweep Schedule: its trigger at start would run every sweep once,
            # beside the test's own (#1095 CI). A test of the sweeps sets its interval.
            "asset_sweep_interval": 0,
        }
    )
    # Before the runs: a Schedule left behind would start more on a queue nobody serves.
    workflow_reaper.delete_schedules(
        schedule_id_for(f"{queue}-library"),
        prune_schedule_id_for(f"{queue}-library"),
        preview_schedule_id_for(f"{queue}-library"),
    )
    workflow_reaper.terminate(queue)
    workflow_reaper.terminate(f"{queue}-bambuddy")
    workflow_reaper.terminate(f"{queue}-library")
    workflow_reaper.terminate(f"{queue}-projects")


class FakeAgentTools:
    """The agent sidecar's flow tools (#2704, `flows/agent_tools.py`) as every api test's
    app reads them: no agent answers on a test's loopback. `tiers` are the enabled tools
    at their tiers now; `down` makes the agent unreachable."""

    def __init__(self) -> None:
        self.tiers: dict[str, str] = {}
        self.down = False

    async def tool_tiers(self) -> dict[str, str]:
        if self.down:
            raise AgentToolsUnavailableError("connection refused")
        return dict(self.tiers)


@pytest.fixture(autouse=True)
def agent_tools(monkeypatch: pytest.MonkeyPatch) -> FakeAgentTools:
    fake = FakeAgentTools()

    async def tool_tiers(_self: AgentTools) -> dict[str, str]:
        return await fake.tool_tiers()

    monkeypatch.setattr(AgentTools, "tool_tiers", tool_tiers)
    return fake


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture(autouse=True)
def _clients_wait_for_the_render_build(
    monkeypatch: pytest.MonkeyPatch, workflow_reaper: WorkflowReaper
) -> None:
    """Every `TestClient` an api test enters (this module's `client`, a module's own,
    one around `create_app`) is handed over once its app's in-process worker's build
    serves the render and library queues: a render started before then waits unrouted, and on a
    loaded host (the dev server's SetCurrentVersion taking 10 s) outlived the submit's
    deadline as a 503. An app with no in-process worker is not waited on."""
    enter = TestClient.__enter__

    def entered(self: TestClient) -> TestClient:
        test_client = enter(self)
        state = getattr(getattr(self.app, "state", None), STATE_ATTR, None)
        settings = state.settings if isinstance(state, AppState) else None
        if settings is None or not settings.temporal_worker_inprocess:
            return test_client
        build_id = settings.revision
        # The library queue too (#4090): its worker is a version of its own deployment,
        # and a command started before that version routes the queue waits unrouted.
        for queue, deployment in (
            (settings.temporal_task_queue_render, DEPLOYMENT_NAME),
            (settings.temporal_task_queue_library, LIBRARY_DEPLOYMENT_NAME),
        ):
            serving = workflow_reaper.run(
                wait_until_build_serves(
                    _reaper_client(workflow_reaper),
                    queue,
                    build_id,
                    timeout=BUILD_SERVES_TIMEOUT,
                    poll=BUILD_SERVES_POLL,
                    max_poll=BUILD_SERVES_MAX_POLL,
                    deployment_name=deployment,
                ),
                # Past the wait's own bound by its last check's describe.
                timeout=BUILD_SERVES_TIMEOUT + 10,
            )
            if not serving:
                self.__exit__(None, None, None)
                pytest.fail(f"build {build_id!r} never came to serve task queue {queue!r}")
        return test_client

    monkeypatch.setattr(TestClient, "__enter__", entered)


def _reaper_client(reaper: WorkflowReaper) -> Client:
    assert reaper.client is not None, "use the reaper as a context manager"
    return reaper.client


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def activity_index(client: TestClient) -> ActivityIndex:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(ACTIVITY).index


class SeedActivity(Protocol):
    def __call__(
        self,
        *,
        id: str,
        principal: str = "browser",
        kind: str = "render",
        status: ActivityStatus = "succeeded",
        **fields: Any,
    ) -> None: ...


@pytest.fixture
def seed_activity(activity_index: ActivityIndex) -> SeedActivity:
    """Write one activity row as a source and its route would: the row, then who
    started it. ``session`` and ``via_tool`` go to the attribution, the rest to the row."""

    def seed(
        *,
        id: str,
        principal: str = "browser",
        kind: str = "render",
        status: ActivityStatus = "succeeded",
        **fields: Any,
    ) -> None:
        session = fields.pop("session", None)
        via_tool = fields.pop("via_tool", None)
        row = ActivityRow(
            id=id,
            kind=kind,
            status=status,
            title=fields.pop("title", f"Seeded {id}"),
            source="test",
            source_id=fields.pop("source_id", id),
            **fields,
        )
        with activity_index.pool.connection() as conn, conn.transaction():
            activity_index.upsert_in(
                conn, row, attribution=Attribution(principal, session, via_tool)
            )

    return seed


#: The agent's tables the backend reads: `ai_operations` and `ai_activity_kinds` as the
#: agent's migrations create them.
AGENT_TABLES = """
CREATE TABLE ai_operations (
  id              text PRIMARY KEY,
  kind            text NOT NULL,
  subject         text NOT NULL,
  operation_key   text NOT NULL,
  status          text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
  request         jsonb NOT NULL,
  result          jsonb,
  error           jsonb,
  workflow_id     text NOT NULL,
  workflow_run_id text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  principal       text,
  session         text,
  via_tool        text,
  CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE TABLE ai_activity_kinds (
  key text PRIMARY KEY, label text NOT NULL, facets jsonb NOT NULL, retired_at timestamptz
);
"""


@dataclass
class AgentTables:
    conninfo: str


@pytest.fixture
def agent_tables(pg_conninfo: str) -> AgentTables:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(AGENT_TABLES.encode())
    return AgentTables(pg_conninfo)


def job_file(client: TestClient, job_id: str, name: str) -> Path:
    """Where a finished job's ``model.3mf`` or ``preview.glb`` is."""
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    result = state.render.store.read(job_id).result
    assert result is not None, f"job {job_id} has no result"
    return (
        state.paths.root / {"model.3mf": result.model_3mf, "preview.glb": result.preview_glb}[name]
    )


def follow_operation(
    client: TestClient, op: dict[str, Any], timeout: float = 60, poll: float = 0.2
) -> dict[str, Any]:
    """Follow a 202's operation (``GET /operations/{id}``) until it is no longer running,
    or ``timeout`` seconds pass; the last read. One loop for every test that follows
    one (#2827), at the 0.2 s most of them polled at (review #3007)."""
    deadline = time.monotonic() + timeout
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(poll)
        response = client.get(f"/api/v1/operations/{op['id']}")
        assert response.status_code == 200, response.text
        op = response.json()
    return op


def set_plate_image(client: TestClient, job_id: str, cover: bytes | None) -> None:
    """Make ``cover`` the finished job's plate image, or leave it none. A real render
    draws its own, except when the cover step times out (and on a loaded machine it
    may), so a test that needs a known image, or none, sets it before saving."""
    path = job_file(client, job_id, "model.3mf")
    with zipfile.ZipFile(path) as archive:
        kept = [
            (info, archive.read(info))
            for info in archive.infolist()
            if not (info.filename.startswith("Metadata/plate_") and info.filename.endswith(".png"))
        ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, data in kept:
            archive.writestr(info, data)
        if cover is not None:
            archive.writestr(PLATE_THUMBNAIL, cover)


def read_stored(conninfo: str) -> dict[str, Any]:
    """What the settings store persisted, read straight from its tables rather than
    through the store: ``settings`` rows by name, plus the per-model choices and
    per-printer plates under the names they have on ``StoredSettings``."""
    with psycopg.connect(conninfo) as conn:
        stored: dict[str, Any] = dict(conn.execute("SELECT name, value FROM settings").fetchall())
        stored["model_print_choices"] = dict(
            conn.execute("SELECT model_id, choices FROM model_print_choices").fetchall()
        )
        stored["printer_bed_types"] = {
            str(printer_id): bed_type
            for printer_id, bed_type in conn.execute(
                "SELECT printer_id, bed_type FROM printer_bed_types"
            ).fetchall()
        }
    return stored


def wait_for_job(client: TestClient, job_id: str, timeout: float = 60) -> dict[str, Any]:
    """The render runs on the app's in-process worker, so poll until it settles."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body: dict[str, Any] = client.get(f"/api/v1/jobs/{job_id}").json()
        if body["status"] in ("done", "failed", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished")
