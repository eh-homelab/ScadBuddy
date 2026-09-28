from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
import trimesh
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, get_render
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.jobs import Job, JobResult, JobStore, PartInfo, RenderQueue
from scadbuddy.render.provenance import source_version
from scadbuddy.render.runner import OpenSCADError
from scadbuddy.render.split import ColourPart

FAIL_WIDTH = 999.0
#: What the failing stub says it could not open (#408).
FAILED_WARNING = "OpenSCAD could not open pic.svg"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake png body"


def set_fake_env(directory: Path, name: str, value: str) -> None:
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


def _fake_result(paths: DataPaths, job: Job) -> JobResult:
    work = paths.job_work_dir(job.id)
    work.mkdir(parents=True, exist_ok=True)
    (work / "preview.glb").write_bytes(b"glTF\x02\x00\x00\x00fake")
    # A real archive, not a stub: saving an output stamps its provenance into the
    # file, and the send path re-places it for the target printer's plate before
    # uploading, so it has to be readable (#105).
    write_bambu_3mf(
        [ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(10, 10, 5)))],
        work / "model.3mf",
        thumbnails=None,
        model_name=job.slug,
    )
    return JobResult(
        model_3mf=str((work / "model.3mf").relative_to(paths.root)),
        preview_glb=str((work / "preview.glb").relative_to(paths.root)),
        # As `render_job` does: the revision the job was resolved to when there is a
        # repository (#90), the content hash only when there is none.
        source_version=job.model_version or source_version(paths.model_dir(job.slug)),
        parts=[PartInfo(name="Color 1", colour="#FF0000", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(10, 10, 5), size=(10, 10, 5)),
        colors=["#FF0000"],
        warnings=["a warning"],
        notes=["a note"],
    )


@pytest.fixture
def app(settings: Settings, paths: DataPaths) -> Iterator[FastAPI]:
    """The real app, with the render step replaced by a stub that writes plausible files.

    ``width: 999`` makes the stub fail, which is how the failed-job paths are reached.
    """
    application = create_app(settings)

    async def fake_render(job: Job) -> tuple[JobResult, list[str]]:
        if job.params.get("width") == FAIL_WIDTH:
            raise OpenSCADError(
                "openscad exited with 1", ["ERROR: something broke"], warnings=[FAILED_WARNING]
            )
        return _fake_result(paths, job), ["rendered fine"]

    queues: dict[str, RenderQueue] = {}

    async def queue_override() -> RenderQueue:
        # Built on first use so its workers live on the app's own event loop.
        if "queue" not in queues:
            # On the app's own registry and bus, as `build_state` wires them, so
            # /metrics reports this queue's jobs and its states are published.
            state = getattr(application.state, STATE_ATTR)
            queue = RenderQueue(
                settings.to_config(),
                paths,
                render=fake_render,
                metrics=state.metrics,
                events=state.events,
            )
            await queue.start()
            queues["queue"] = queue
        return queues["queue"]

    application.dependency_overrides[get_render] = queue_override
    yield application
    # The workers die with the TestClient's loop; the thumbnail pool is threads, so
    # it is released here rather than left for interpreter exit.
    for queue in queues.values():
        queue.close_thumbnails()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def job_file(paths: DataPaths, job_id: str, name: str) -> Path:
    """Where a finished job's ``model.3mf`` or ``preview.glb`` is: under the template
    once the worker kept the render (`render_cache`), the work directory otherwise."""
    result = JobStore(paths).read(job_id).result
    assert result is not None, f"job {job_id} has no result"
    return paths.root / {"model.3mf": result.model_3mf, "preview.glb": result.preview_glb}[name]


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


def wait_for_job(client: TestClient, job_id: str) -> dict[str, Any]:
    """The stub render resolves on the queue's worker, so poll until it settles."""
    for _ in range(200):
        body: dict[str, Any] = client.get(f"/api/v1/jobs/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished")
