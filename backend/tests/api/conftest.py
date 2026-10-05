from __future__ import annotations

import json
import os
import time
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
import trimesh
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.render.bambu3mf import PLATE_THUMBNAIL
from tests.conftest import write_openscad_3mf
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
            "temporal_worker_inprocess": True,
        }
    )
    workflow_reaper.terminate(queue)
    workflow_reaper.terminate(f"{queue}-bambuddy")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def job_file(client: TestClient, job_id: str, name: str) -> Path:
    """Where a finished job's ``model.3mf`` or ``preview.glb`` is."""
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    result = state.render.store.read(job_id).result
    assert result is not None, f"job {job_id} has no result"
    return (
        state.paths.root / {"model.3mf": result.model_3mf, "preview.glb": result.preview_glb}[name]
    )


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
