from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import trimesh
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import get_queue
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.jobs import Job, JobResult, PartInfo, RenderQueue
from scadbuddy.render.provenance import source_version
from scadbuddy.render.runner import OpenSCADError
from scadbuddy.render.split import ColourPart

MODEL_SLUG = "demo"
FAIL_WIDTH = 999.0

# A stand-in for the real binary: enough to answer --version and to export a .param,
# so the routes that shell out are exercised where no openscad is installed.
FAKE_OPENSCAD = """#!/usr/bin/env python3
import json
import pathlib
import sys

args = sys.argv[1:]
if "--version" in args:
    print("OpenSCAD version 2099.01.01", file=sys.stderr)  # the real one uses stderr too
    raise SystemExit(0)

out = None
for index, arg in enumerate(args):
    if arg == "-o" and index + 1 < len(args):
        out = args[index + 1]

source = pathlib.Path(args[-1])
text = source.read_text(encoding="utf-8", errors="replace") if source.is_file() else ""
if "%%FAIL%%" in text:
    print("ERROR: Parser error: syntax error", file=sys.stderr)
    raise SystemExit(1)

if out is not None and out.endswith(".param"):
    pathlib.Path(out).write_text(
        json.dumps(
            {
                "title": "Fake",
                "parameters": [
                    {"name": "width", "type": "number", "initial": 10, "group": "Main"},
                    {"name": "label", "type": "string", "initial": "hi", "group": "Main"},
                ],
            }
        )
    )
raise SystemExit(0)
"""

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake png body"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray SCADBUDDY_* on the developer's machine must not reach the app."""
    for key in list(os.environ):
        if key.startswith("SCADBUDDY_"):
            monkeypatch.delenv(key)


@pytest.fixture
def fake_openscad(tmp_path: Path) -> str:
    binary = tmp_path / "fake-openscad"
    binary.write_text(FAKE_OPENSCAD, encoding="utf-8")
    binary.chmod(0o755)
    return str(binary)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def seed_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "seed"
    directory.mkdir()
    return directory


@pytest.fixture
def settings(data_dir: Path, seed_dir: Path, fake_openscad: str) -> Settings:
    return Settings(
        openscad=fake_openscad,
        data_dir=data_dir,
        seed_models_dir=seed_dir,
        frontend_dir=Path("/nonexistent"),
    )


@pytest.fixture
def paths(data_dir: Path) -> DataPaths:
    data = DataPaths(data_dir)
    data.ensure()
    return data


@pytest.fixture
def model(paths: DataPaths) -> str:
    paths.model_dir(MODEL_SLUG).mkdir(parents=True, exist_ok=True)
    paths.model_source(MODEL_SLUG).write_text('width = 10;\nlabel = "hi";\n', encoding="utf-8")
    paths.model_meta(MODEL_SLUG).write_text(
        json.dumps({"name": "Demo", "description": "a demo", "tags": ["test"]}) + "\n",
        encoding="utf-8",
    )
    return MODEL_SLUG


def _fake_result(paths: DataPaths, job: Job) -> JobResult:
    work = paths.job_work_dir(job.id)
    work.mkdir(parents=True, exist_ok=True)
    (work / "preview.glb").write_bytes(b"glTF\x02\x00\x00\x00fake")
    # A real 3MF, because saving an output stamps its provenance into the file.
    write_bambu_3mf(
        [ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(10, 10, 5)))],
        work / "model.3mf",
        thumbnails=None,
        model_name=job.slug,
    )
    return JobResult(
        model_3mf=str((work / "model.3mf").relative_to(paths.root)),
        preview_glb=str((work / "preview.glb").relative_to(paths.root)),
        source_version=source_version(paths.model_dir(job.slug)),
        parts=[PartInfo(name="Color 1", colour="#FF0000", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(10, 10, 5), size=(10, 10, 5)),
        colors=["#FF0000"],
        warnings=["a warning"],
    )


@pytest.fixture
def app(settings: Settings, paths: DataPaths) -> FastAPI:
    """The real app, with the render step replaced by a stub that writes plausible files.

    ``width: 999`` makes the stub fail, which is how the failed-job paths are reached.
    """
    application = create_app(settings)

    async def fake_render(job: Job) -> tuple[JobResult, list[str]]:
        if job.params.get("width") == FAIL_WIDTH:
            raise OpenSCADError("openscad exited with 1", ["ERROR: something broke"])
        return _fake_result(paths, job), ["rendered fine"]

    queues: dict[str, RenderQueue] = {}

    async def queue_override() -> RenderQueue:
        # Built on first use so its workers live on the app's own event loop.
        if "queue" not in queues:
            queue = RenderQueue(settings.to_config(), paths, render=fake_render)
            await queue.start()
            queues["queue"] = queue
        return queues["queue"]

    application.dependency_overrides[get_queue] = queue_override
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def wait_for_job(client: TestClient, job_id: str) -> dict[str, Any]:
    """The stub render resolves on the queue's worker, so poll until it settles."""
    for _ in range(200):
        body: dict[str, Any] = client.get(f"/api/v1/jobs/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished")
