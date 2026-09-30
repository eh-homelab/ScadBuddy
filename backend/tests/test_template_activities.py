"""pipeline/activities.py in a subprocess (spec 2026-09-27 §5.2, §3.4, §9)."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.template import Blob
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.models import TemplateCall
from scadbuddy.workflows.pipeline_activities import PipelineActivities

ACTIVITIES = """\
import os, subprocess, time
from scadbuddy.template import emit

def add(a, b):
    return {"sum": a + b}

async def later(x):
    return x * 2

def environment():
    return sorted(os.environ)

def guide(rooms):
    return emit("guide.svg", f"<svg>{rooms}</svg>")

def read(blob):
    return blob.read_bytes().decode()

def boom():
    x = 1
    raise ValueError("no rooms")

def huge():
    return "x" * (2 << 20)

def spawn(pid_file):
    child = subprocess.Popen(["sleep", "300"])
    with open(pid_file, "w") as f:
        f.write(f"{os.getpid()} {child.pid}")
    time.sleep(300)

def leave_child():
    subprocess.Popen(["sleep", "300"])
    return "done"
"""


def _world(tmp_path: Path) -> tuple[PipelineActivities, DataPaths]:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("cube();\n", encoding="utf-8")
    paths.model_meta("demo").write_text('{"name": "Demo"}', encoding="utf-8")
    (paths.model_dir("demo") / "pipeline").mkdir()
    (paths.model_dir("demo") / "pipeline" / "activities.py").write_text(
        ACTIVITIES, encoding="utf-8"
    )
    deps = WorkerDeps(
        config=Config(data_dir=paths.root),
        paths=paths,
        assets=AssetStore(paths.assets),
        blobs=LocalBlobStore(paths.blobs),
        refs=None,  # type: ignore[arg-type]
        projection=None,  # type: ignore[arg-type]
        template_python=sys.executable,
    )
    return PipelineActivities(deps), paths


def _call(name: str, *args: object, timeout_s: float = 60, **kwargs: object) -> TemplateCall:
    return TemplateCall(
        slug="demo",
        revision=None,
        name=name,
        args=list(args),
        kwargs=dict(kwargs),
        timeout_s=timeout_s,
    )


async def test_a_function_runs_with_json_in_and_out(tmp_path: Path) -> None:
    acts, _ = _world(tmp_path)
    env = ActivityEnvironment()
    assert await env.run(acts.run_template_activity, _call("add", 2, b=3)) == {"sum": 5}
    assert await env.run(acts.run_template_activity, _call("later", 21)) == 42


async def test_the_process_sees_no_worker_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCADBUDDY_DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("SCADBUDDY_SECRET_KEY_FILE", "/run/secret")
    acts, _ = _world(tmp_path)
    names = await ActivityEnvironment().run(acts.run_template_activity, _call("environment"))
    assert not [
        n for n in names if n.startswith("SCADBUDDY_") and not n.startswith("SCADBUDDY_TEMPLATE_")
    ]


async def test_an_emitted_file_comes_back_as_a_blob_and_can_be_read_again(tmp_path: Path) -> None:
    acts, paths = _world(tmp_path)
    env = ActivityEnvironment()
    blob = Blob.model_validate(await env.run(acts.run_template_activity, _call("guide", 2)))
    assert (paths.blobs / blob.key / "guide.svg").read_text() == "<svg>2</svg>"
    assert (
        await env.run(acts.run_template_activity, _call("read", blob.model_dump()))
        == "<svg>2</svg>"
    )


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("boom", "pipeline/activities.py:21: ValueError: no rooms"),
        ("missing", "pipeline/activities.py has no function 'missing'"),
        ("_private", "pipeline/activities.py has no function '_private'"),
        ("huge", "return scadbuddy.template.emit"),
    ],
)
async def test_a_template_error_is_non_retryable_and_names_its_line(
    tmp_path: Path, name: str, message: str
) -> None:
    acts, _ = _world(tmp_path)
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(acts.run_template_activity, _call(name))
    assert raised.value.type == "TemplateActivityError" and raised.value.non_retryable
    assert message in raised.value.message


def _dead(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0]
    except FileNotFoundError:
        return True
    return state == "Z"


async def test_cancelling_kills_the_template_process_group(tmp_path: Path) -> None:
    acts, _ = _world(tmp_path)
    pid_file = tmp_path / "pids"
    env = ActivityEnvironment()
    task = asyncio.create_task(env.run(acts.run_template_activity, _call("spawn", str(pid_file))))
    deadline = time.monotonic() + 30
    while not pid_file.is_file() or not pid_file.read_text():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    pids = [int(p) for p in pid_file.read_text().split()]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    deadline = time.monotonic() + 5
    while not all(_dead(pid) for pid in pids):
        assert time.monotonic() < deadline, f"still alive: {pids}"
        await asyncio.sleep(0.05)


async def test_a_timed_out_function_is_killed_and_refused(tmp_path: Path) -> None:
    acts, _ = _world(tmp_path)
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(
            acts.run_template_activity, _call("spawn", str(tmp_path / "p"), timeout_s=1)
        )
    assert "timed out after 1s" in raised.value.message


async def test_a_returned_function_does_not_wait_for_its_children(tmp_path: Path) -> None:
    acts, _ = _world(tmp_path)
    started = time.monotonic()
    assert (
        await ActivityEnvironment().run(acts.run_template_activity, _call("leave_child")) == "done"
    )
    assert time.monotonic() - started < 20
