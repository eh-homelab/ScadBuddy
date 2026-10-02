"""pipeline/activities.py in a subprocess (spec 2026-09-27 §5.2, §3.4, §9)."""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.store import BlobStore
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.template import Blob
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.models import Failure, TemplateCall
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from scadbuddy.workflows.template_process import OUTPUT_LOG_MAX_BYTES
from tests.support.temporal import temporal_client

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

def leave_child(pid_file):
    child = subprocess.Popen(["sleep", "300"])
    with open(pid_file, "w") as f:
        f.write(str(child.pid))
    return "done"

def aset():
    return {1, 2}

def counted(path):
    with open(path, "a") as f:
        f.write("x")
    return emit("n.txt", "n")

def slow(seconds):
    time.sleep(seconds)
    return "slept"

def helper_value():
    import helper

    return helper.VALUE

def chatty():
    for i in range(3000):
        print(f"line {i} " + "x" * 1000)
    raise ValueError("too chatty")
"""


class RecordingRefs:
    def __init__(self) -> None:
        self.held: set[tuple[str, str, str]] = set()

    def add(self, key: str, holder_kind: str, holder_id: str) -> None:
        self.held.add((key, holder_kind, holder_id))

    def referenced(self) -> set[str]:
        return {key for key, _, _ in self.held}


def _world(
    tmp_path: Path, *, blobs: BlobStore | None = None, refs: RecordingRefs | None = None
) -> tuple[PipelineActivities, DataPaths]:
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
        blobs=blobs or LocalBlobStore(paths.blobs),
        refs=refs or RecordingRefs(),  # type: ignore[arg-type]
        projection=None,  # type: ignore[arg-type]
        template_python=sys.executable,
    )
    return PipelineActivities(deps), paths


def _call(
    name: str, *args: object, timeout_s: float = 60, job_id: str = "job-1", **kwargs: object
) -> TemplateCall:
    return TemplateCall(
        slug="demo",
        revision=None,
        name=name,
        args=list(args),
        kwargs=dict(kwargs),
        timeout_s=timeout_s,
        job_id=job_id,
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
        ("aset", "aset() returned a value that is not JSON"),
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
        await asyncio.wait_for(task, 10)  # a group left alive would hang it
    deadline = time.monotonic() + 5
    while not all(_dead(pid) for pid in pids):
        assert time.monotonic() < deadline, f"still alive: {pids}"
        await asyncio.sleep(0.05)


async def _until_dead(pids: list[int], within: float = 5) -> None:
    deadline = time.monotonic() + within
    while not all(_dead(pid) for pid in pids):
        assert time.monotonic() < deadline, f"still alive: {pids}"
        await asyncio.sleep(0.05)


async def test_a_timed_out_function_is_killed_and_refused(tmp_path: Path) -> None:
    acts, _ = _world(tmp_path)
    pid_file = tmp_path / "p"
    with pytest.raises(ApplicationError) as raised:
        await asyncio.wait_for(
            ActivityEnvironment().run(
                acts.run_template_activity, _call("spawn", str(pid_file), timeout_s=5)
            ),
            30,
        )
    assert "timed out after 5s" in raised.value.message
    # The function and the child it started: the whole group.
    await _until_dead([int(p) for p in pid_file.read_text().split()])


async def test_a_returned_function_does_not_wait_for_its_children(tmp_path: Path) -> None:
    acts, _ = _world(tmp_path)
    pid_file = tmp_path / "child"
    started = time.monotonic()
    assert (
        await asyncio.wait_for(
            ActivityEnvironment().run(
                acts.run_template_activity, _call("leave_child", str(pid_file))
            ),
            30,
        )
        == "done"
    )
    assert time.monotonic() - started < 20
    # What it left running goes with its group.
    await _until_dead([int(pid_file.read_text())])


async def test_an_emitted_blob_is_held_for_the_job(tmp_path: Path) -> None:
    refs = RecordingRefs()
    acts, _ = _world(tmp_path, refs=refs)
    env = ActivityEnvironment()
    blob = Blob.model_validate(
        await env.run(acts.run_template_activity, _call("guide", 2, job_id="j1"))
    )
    assert (blob.key, "job", "j1") in refs.held
    # A second job's identical call reuses the blob, and holds it too.
    again = await env.run(acts.run_template_activity, _call("guide", 2, job_id="j2"))
    assert Blob.model_validate(again) == blob
    assert (blob.key, "job", "j2") in refs.held


async def test_identical_calls_at_once_on_one_worker_run_once(tmp_path: Path) -> None:
    acts, _ = _world(tmp_path)
    count = tmp_path / "count"
    env = ActivityEnvironment()
    first, second = await asyncio.gather(
        env.run(acts.run_template_activity, _call("counted", str(count))),
        env.run(acts.run_template_activity, _call("counted", str(count))),
    )
    assert first == second
    assert count.read_text() == "x"
    # The lock went with the last call: the map holds only calls in flight.
    assert acts._calls == {}


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "blob", "key": "act-nope", "path": "guide.svg"},
        {"kind": "part", "piece_key": "nope"},
    ],
)
async def test_a_blob_the_store_does_not_hold_is_refused_before_the_call(
    tmp_path: Path, value: dict[str, str]
) -> None:
    acts, _ = _world(tmp_path)
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(acts.run_template_activity, _call("read", value))
    assert raised.value.type == "TemplateActivityError" and raised.value.non_retryable
    assert "is not in the store" in raised.value.message


async def test_a_chatty_function_keeps_only_the_end_of_its_log(tmp_path: Path) -> None:
    acts, _ = _world(tmp_path)
    with pytest.raises(ApplicationError) as raised:
        await asyncio.wait_for(
            ActivityEnvironment().run(acts.run_template_activity, _call("chatty")), 60
        )
    failure = raised.value.details[0]
    assert isinstance(failure, Failure)
    tail = "\n".join(failure.log_tail)
    # About 3 MiB printed: only the last OUTPUT_LOG_MAX_BYTES are kept, and it says so.
    assert "bytes of output were dropped" in tail
    assert OUTPUT_LOG_MAX_BYTES == 1 << 20
    assert failure.log_tail[-1].startswith("line 2999 ")
    assert "ValueError: too chatty" in raised.value.message


@workflow.defn(name="CallsATemplateActivity", sandboxed=False)
class _CallsATemplateActivity:
    @workflow.run
    async def run(self, call: TemplateCall) -> Any:
        return await workflow.execute_activity(
            "run_template_activity",
            call,
            start_to_close_timeout=timedelta(seconds=60),
            heartbeat_timeout=timedelta(seconds=8),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@pytest.mark.requires_temporal
async def test_a_call_longer_than_its_heartbeat_timeout_survives(tmp_path: Path) -> None:
    """`_heartbeating` beats every 5 s; a 12 s call outlives an 8 s heartbeat timeout."""
    acts, _ = _world(tmp_path)
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(
            client,
            task_queue=queue,
            workflows=[_CallsATemplateActivity],
            activities=[acts.run_template_activity],
        ):
            result = await asyncio.wait_for(
                client.execute_workflow(
                    _CallsATemplateActivity.run,
                    _call("slow", 12),
                    id=f"calls-{uuid.uuid4().hex[:8]}",
                    task_queue=queue,
                ),
                60,
            )
    assert result == "slept"


async def test_editing_a_module_activities_imports_changes_the_call(tmp_path: Path) -> None:
    """A live template's `act-` key covers every `*.py` under `pipeline/`, not
    `activities.py` alone: an edited sibling it imports is never answered from the
    store with the old result."""
    acts, paths = _world(tmp_path)
    helper = paths.model_dir("demo") / "pipeline" / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    env = ActivityEnvironment()
    assert await env.run(acts.run_template_activity, _call("helper_value")) == 1
    helper.write_text("VALUE = 2\n", encoding="utf-8")
    assert await env.run(acts.run_template_activity, _call("helper_value")) == 2


class _SlowFetch(LocalBlobStore):
    """A store whose fetch of one key is a long download, as on the bambuddy store."""

    def __init__(self, root: Path, slow: set[str], seconds: float) -> None:
        super().__init__(root)
        self.slow, self.seconds = slow, seconds

    async def fetch(self, key: str) -> bool:
        if key in self.slow:
            await asyncio.sleep(self.seconds)
        return await super().fetch(key)


@pytest.mark.requires_temporal
async def test_a_blob_argument_slower_than_the_heartbeat_timeout_still_arrives(
    tmp_path: Path,
) -> None:
    """Bringing in a Blob argument beats while it downloads (final re-review): a 12 s
    fetch outlives the 8 s heartbeat timeout `_CallsATemplateActivity` gives it."""
    paths = DataPaths(tmp_path / "data")
    store = _SlowFetch(paths.blobs, set(), 12.0)
    acts, _ = _world(tmp_path, blobs=store)
    blob = Blob.model_validate(
        await ActivityEnvironment().run(acts.run_template_activity, _call("guide", 3))
    )
    store.slow.add(blob.key)
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(
            client,
            task_queue=queue,
            workflows=[_CallsATemplateActivity],
            activities=[acts.run_template_activity],
        ):
            result = await asyncio.wait_for(
                client.execute_workflow(
                    _CallsATemplateActivity.run,
                    _call("read", blob.model_dump(mode="json")),
                    id=f"calls-{uuid.uuid4().hex[:8]}",
                    task_queue=queue,
                ),
                60,
            )
    assert result == "<svg>3</svg>"
