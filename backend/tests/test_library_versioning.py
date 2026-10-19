"""The API's ``library`` worker is a Worker Deployment version (#4090): an old replica
beside a new one during a rolling update never takes a task only the new build has."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.worker import WorkerDeploymentConfig, WorkerDeploymentVersion

from scadbuddy import main
from scadbuddy.worker import make_current_until_polled
from scadbuddy.workflows.client import (
    LIBRARY_DEPLOYMENT_NAME,
    library_deployment,
    make_current,
    sandboxed_runner,
)
from scadbuddy.workflows.housekeeping import (
    Housekeeping,
    VersionedHousekeeping,
    library_worker,
)
from scadbuddy.workflows.previews import PreviewBackfill
from tests.support.deployment import wait_until_build_serves
from tests.support.temporal import temporal_client, terminate_open_workflows
from tests.test_print_worker_task import StubWorker


def _library_state(revision: str) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            temporal_task_queue_library="library",
            revision=revision,
            spool_overlay_refresh_seconds=0,
        ),
        temporal=object(),
        config=SimpleNamespace(asset_sweep_interval=0),
        previews=None,
        components=SimpleNamespace(get=lambda key: SimpleNamespace(store=None, kinds={})),
        settings_store=None,
    )


async def _run_once(monkeypatch: pytest.MonkeyPatch, revision: str) -> tuple[dict[str, Any], str]:
    """Start `main._run_library_worker` until its worker is built and its build made
    current; what the worker was built with, and the build it made current."""
    built: dict[str, Any] = {}
    made: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def build(*args: Any, **kwargs: Any) -> StubWorker:
        built.update(kwargs)
        return StubWorker(False, asyncio.Event())

    async def current(client: object, build_id: str, queue: str, routed: asyncio.Event) -> None:
        made.set_result(build_id)
        routed.set()

    async def no_schedules(*args: Any) -> bool:
        return False

    async def no_triggers() -> None:
        pass

    monkeypatch.setattr(main, "library_worker", build)
    monkeypatch.setattr(main, "_make_library_current", current)
    monkeypatch.setattr(main, "ensure_schedules", no_schedules)
    monkeypatch.setattr(main, "_housekeeping_activities", lambda state: [])
    monkeypatch.setattr(main, "_flow_triggers", lambda *args: ([], no_triggers))
    stop = asyncio.Event()
    task = asyncio.create_task(main._run_library_worker(_library_state(revision), stop))  # type: ignore[arg-type]
    made_current = await asyncio.wait_for(made, 5)
    stop.set()
    await asyncio.wait_for(task, 5)
    return built, made_current


async def test_the_library_worker_is_this_builds_version_and_makes_it_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built, made_current = await _run_once(monkeypatch, "abc123")
    config = built["deployment_config"]
    assert isinstance(config, WorkerDeploymentConfig)
    assert config.use_worker_versioning
    assert config.version == WorkerDeploymentVersion(
        deployment_name=LIBRARY_DEPLOYMENT_NAME, build_id="abc123"
    )
    # No default: each workflow names its own, or the SDK refuses to build the worker.
    assert config.default_versioning_behavior == VersioningBehavior.UNSPECIFIED
    assert made_current == "abc123"
    assert tuple(built["workflows"]) == main.LIBRARY_WORKFLOWS


async def test_the_schedules_wait_for_this_build_to_route_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each set-up triggers a run with this build's sweeps: set up while the previous
    build still has the queue, an old replica would take a sweep it lacks (#4090)."""
    gate = asyncio.Event()
    set_up = asyncio.Event()
    order: list[str] = []

    async def current(client: object, build_id: str, queue: str, routed: asyncio.Event) -> None:
        await gate.wait()
        order.append("routed")
        routed.set()

    async def schedules(*args: Any) -> bool:
        order.append("schedules")
        set_up.set()
        return False

    async def no_preview_schedule(*args: Any) -> None:
        pass

    async def no_triggers() -> None:
        pass

    monkeypatch.setattr(main, "library_worker", lambda *a, **k: StubWorker(False, asyncio.Event()))
    monkeypatch.setattr(main, "_make_library_current", current)
    monkeypatch.setattr(main, "ensure_schedules", schedules)
    monkeypatch.setattr(main, "ensure_preview_schedule", no_preview_schedule)
    monkeypatch.setattr(main, "ensure_spool_schedule", no_preview_schedule)
    monkeypatch.setattr(main, "_housekeeping_activities", lambda state: [])
    monkeypatch.setattr(main, "_flow_triggers", lambda *args: ([], no_triggers))
    stop = asyncio.Event()
    task = asyncio.create_task(main._run_library_worker(_library_state("abc123"), stop))  # type: ignore[arg-type]
    for _ in range(20):
        await asyncio.sleep(0)
    assert order == []
    gate.set()
    await asyncio.wait_for(set_up.wait(), 5)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert order == ["routed", "schedules"]


async def test_a_restarted_worker_does_not_make_its_build_current_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #4101: a worker restarted (a poller lost in a Temporal blip) late in a
    rollout, after a newer build became current, must not write its own build back. The
    build is made current once per process, not per worker start."""
    second = asyncio.Event()
    built: list[StubWorker] = []
    made: list[str] = []

    def build(*args: Any, **kwargs: Any) -> StubWorker:
        built.append(StubWorker(False, second if built else asyncio.Event(), fail_after=not built))
        return built[-1]

    async def current(client: object, build_id: str, queue: str, routed: asyncio.Event) -> None:
        made.append(build_id)
        routed.set()

    async def no_schedules(*args: Any) -> bool:
        return False

    async def no_triggers() -> None:
        pass

    monkeypatch.setattr(main, "library_worker", build)
    monkeypatch.setattr(main, "_make_library_current", current)
    monkeypatch.setattr(main, "PRINT_WORKER_RECONNECT", 0.01)
    monkeypatch.setattr(main, "ensure_schedules", no_schedules)
    monkeypatch.setattr(main, "_housekeeping_activities", lambda state: [])
    monkeypatch.setattr(main, "_flow_triggers", lambda *args: ([], no_triggers))
    stop = asyncio.Event()
    task = asyncio.create_task(main._run_library_worker(_library_state("abc123"), stop))  # type: ignore[arg-type]
    await asyncio.wait_for(second.wait(), 5)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert len(built) == 2
    assert made == ["abc123"]


def test_every_library_workflow_names_its_versioning_behavior() -> None:
    """Each type is AUTO_UPGRADE, chosen per class (#4090): a Kubernetes rollout stops
    an old replica within its termination grace, so a Pinned run it held would be
    stranded. The names are the queue's whole set, so a new one is chosen here too."""
    behaviors = {
        definition.name: definition.versioning_behavior
        for definition in (
            workflow._Definition.must_from_class(cls)
            for cls in (VersionedHousekeeping, *main.LIBRARY_WORKFLOWS)
        )
    }
    assert behaviors == {
        "Housekeeping": VersioningBehavior.AUTO_UPGRADE,
        "PreviewBackfill": VersioningBehavior.AUTO_UPGRADE,
        "Operation": VersioningBehavior.AUTO_UPGRADE,
        "FlowTriggerFire": VersioningBehavior.AUTO_UPGRADE,
        "StoreMigration": VersioningBehavior.AUTO_UPGRADE,
        "SliceWorkflow": VersioningBehavior.AUTO_UPGRADE,
        "SpoolRefresh": VersioningBehavior.AUTO_UPGRADE,
    }


# ── mixed builds on one queue, as a rolling update runs them ──────────────────────

#: The sweep the new build adds; the old build does not register it.
NEW_SWEEP = "housekeeping_new_in_this_build"
BLOCK_SWEEP = "housekeeping_blocks"


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.mark.requires_temporal
async def test_the_versioned_worker_refuses_a_workflow_without_a_behavior(
    client: Client,
) -> None:
    with pytest.raises(
        ValueError, match="Workflow PreviewBackfill must specify a versioning behavior"
    ):
        library_worker(
            client,
            "library",
            [],
            workflows=[PreviewBackfill],
            workflow_runner=sandboxed_runner(),
            deployment_config=library_deployment("b"),
        )


def _deployment(name: str, build_id: str) -> WorkerDeploymentConfig:
    # A deployment of the test's own, so no other test's current build moves it.
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(deployment_name=name, build_id=build_id),
        use_worker_versioning=True,
    )


async def _make(client: Client, name: str, build_id: str) -> None:
    await asyncio.wait_for(
        make_current_until_polled(
            lambda: make_current(
                client, namespace=client.namespace, build_id=build_id, deployment_name=name
            ),
            build_id=build_id,
            backoff=(0.1,),
            every=0.2,
            error_after=30,
        ),
        30,
    )


@pytest.mark.requires_temporal
async def test_a_run_across_a_rollout_takes_the_new_sweep_on_the_new_build(
    client: Client,
) -> None:
    """The rolling update #4090 makes safe: an old and a new replica both poll the
    queue. A run started on the old build moves to the new one once it is current
    (AUTO_UPGRADE), and the sweep only the new build registers is never handed to the
    old one, which would fail it as unregistered. Unversioned, either replica could take
    it."""
    name = f"scadbuddy-library-test-{uuid.uuid4().hex[:8]}"
    queue = f"library-{uuid.uuid4().hex[:8]}"
    blocking = asyncio.Event()
    release = asyncio.Event()
    ran: list[tuple[str, str]] = []

    def blocks(build: str) -> Any:
        @activity.defn(name=BLOCK_SWEEP)
        async def sweep() -> None:
            ran.append((BLOCK_SWEEP, build))
            blocking.set()
            await release.wait()

        return sweep

    @activity.defn(name=NEW_SWEEP)
    async def new_sweep() -> None:
        ran.append((NEW_SWEEP, "new"))

    old = library_worker(
        client,
        queue,
        [blocks("old")],
        workflow_runner=sandboxed_runner(),
        deployment_config=_deployment(name, "old"),
    )
    new = library_worker(
        client,
        queue,
        [blocks("new"), new_sweep],
        workflow_runner=sandboxed_runner(),
        deployment_config=_deployment(name, "new"),
    )
    try:
        async with old:
            await _make(client, name, "old")
            handle = await client.start_workflow(
                Housekeeping.run,
                [BLOCK_SWEEP, NEW_SWEEP],
                id=f"housekeeping-{uuid.uuid4().hex}",
                task_queue=queue,
            )
            await asyncio.wait_for(blocking.wait(), 30)
            async with new:
                await _make(client, name, "new")
                # As `main._make_library_current` waits: released before the queue
                # hears of the new current version (0.2-0.3 s here), the run's next
                # sweep was handed to the old build and failed as unregistered.
                assert await wait_until_build_serves(
                    client, queue, "new", timeout=30, poll=0.05, max_poll=0.5, deployment_name=name
                )
                release.set()
                failed = await asyncio.wait_for(handle.result(), 60)
    finally:
        await terminate_open_workflows(client, queue)
    assert failed == []
    assert ran == [(BLOCK_SWEEP, "old"), (NEW_SWEEP, "new")]
