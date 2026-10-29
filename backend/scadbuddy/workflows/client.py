from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio.api.common.v1 import Payload
from temporalio.api.deployment.v1 import WorkerDeploymentVersion as DeploymentVersion
from temporalio.api.enums.v1 import TaskQueueKind, TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import (
    CountWorkflowExecutionsRequest,
    DescribeTaskQueueRequest,
    DescribeWorkerDeploymentRequest,
    DescribeWorkerDeploymentVersionRequest,
    SetWorkerDeploymentCurrentVersionRequest,
    UpdateWorkerDeploymentVersionMetadataRequest,
)
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import VersioningBehavior
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.runtime import Runtime
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker, WorkerDeploymentConfig, WorkerDeploymentVersion
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from scadbuddy.bambuddy.follow import FOLLOW_SLOTS
from scadbuddy.bambuddy.runs import PrintRunStore
from scadbuddy.operations.store import OperationStore
from scadbuddy.workflows.activities import RenderActivities
from scadbuddy.workflows.commands import (
    CONNECT_MARGIN_SECONDS,
    DESCRIBE_SECONDS,
    TemporalRefusedError,
    TemporalUnavailableError,
    namespace_not_found,
    temporal_failure,
)
from scadbuddy.workflows.follow import FollowPrint, VersionedFollowPrint, follow_queue
from scadbuddy.workflows.operation import OperationWorkflow
from scadbuddy.workflows.operation_models import HEARTBEAT_THROTTLE
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from scadbuddy.workflows.pipelines import (
    MigrateInputs,
    RenderPiece,
    RenderPreview,
    TemplatePipeline,
)
from scadbuddy.workflows.printer_watch import PrinterWatch
from scadbuddy.workflows.printing import PrintRunWorkflow
from scadbuddy.workflows.problems import operation_lost
from scadbuddy.workflows.registered import RegisteredActivities

RENDER_TASK_QUEUE_DEFAULT = "render"
DEPLOYMENT_NAME = "scadbuddy-render"
#: The print worker's deployment (#1060, spec 2026-10-01 §5.5): `--queue bambuddy`.
PRINT_DEPLOYMENT_NAME = "scadbuddy-print"
#: The API's ``library`` worker's deployment (#4090): housekeeping, the preview
#: backfill, the library commands, flow trigger fires and the store migration.
LIBRARY_DEPLOYMENT_NAME = "scadbuddy-library"
#: The flow runs' worker's deployment (#4146): `--queue projects`.
PROJECTS_DEPLOYMENT_NAME = "scadbuddy-projects"
RPC_TIMEOUT = timedelta(seconds=10)
#: How recently a poller must have polled to count as another worker still serving the
#: queue (#4869). A worker's long poll waits up to about 60 s with no task, and
#: `last_access_time` moves only when a poll arrives, so a live idle worker can look 60
#: to 70 s old.
POLLER_SEEN_WITHIN = 90.0
#: A draining worker's mark in its build version's metadata (#4948): this prefix and the
#: worker's identity, the value when its drain began (`drain_mark`). A draining worker
#: keeps polling, so its polls alone cannot tell it from one that serves.
DRAINING_MARK = "draining:"
#: The unparsable marks already logged (`draining_marks`).
_unparsable_marks: set[str] = set()

logger = logging.getLogger(__name__)


async def connect(
    address: str, namespace: str, *, lazy: bool = False, runtime: Runtime | None = None
) -> Client:
    """``lazy`` connects on the first call instead of here (the API, which must boot
    with Temporal down); the worker connects eagerly and fails fast. Every client
    traces (spec 2026-10-01 §4): context rides in workflow headers, and a worker built
    on this client takes the same interceptor."""
    return await Client.connect(
        address,
        namespace=namespace,
        data_converter=pydantic_data_converter,
        lazy=lazy,
        interceptors=[TracingInterceptor()],
        runtime=runtime,
    )


def connect_lazily(address: str, namespace: str) -> Client:
    """The API's client, built synchronously: `create_app` runs inside uvicorn's loop,
    where nothing can be awaited. A lazy connect never suspends, so driving the
    coroutine once returns the client; it connects on its first call."""
    coro = connect(address, namespace, lazy=True)
    try:
        coro.send(None)
    except StopIteration as done:
        client: Client = done.value
        return client
    coro.close()
    raise RuntimeError("a lazy Temporal connect suspended")


def sandboxed_runner() -> SandboxedWorkflowRunner:
    """Every worker's workflow sandbox: the render, ``bambuddy`` and ``library`` workers
    (#2014). The client's tracing interceptor makes workflow spans inside it, and
    OpenTelemetry's module state must be the process's, not a sandboxed copy.
    tests/test_trace_scrub.py runs a workflow under this same runner (#1810)."""
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules("opentelemetry")
    )


def render_worker(
    client: Client,
    task_queue: str,
    activities: RenderActivities,
    *,
    pipeline: PipelineActivities,
    build_id: str,
    max_concurrent_activities: int,
    graceful_shutdown_timeout: timedelta = timedelta(),
) -> Worker:
    """The render worker (spec §3.5): versioned by build id so a rolling deploy lets
    old workers drain the workflows they started."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[TemplatePipeline, RenderPiece, RenderPreview, MigrateInputs],
        activities=[*activities.all(), *pipeline.all()],
        workflow_runner=sandboxed_runner(),
        max_concurrent_activities=max_concurrent_activities,
        graceful_shutdown_timeout=graceful_shutdown_timeout,
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name=DEPLOYMENT_NAME, build_id=build_id),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        ),
    )


def bambuddy_worker(
    client: Client,
    task_queue: str,
    activities: Sequence[Callable[..., Any]],
    *,
    graceful_shutdown_timeout: timedelta = timedelta(seconds=30),
    build_id: str | None = None,
) -> Worker:
    """The ``bambuddy`` worker (#1052, #1053, spec 2026-10-01 §5.5): ``PrintRun``,
    ``Operation`` and ``FollowPrint`` (whose activity `follow_worker` serves).
    With ``build_id`` (the ``scadbuddy-print`` Deployment, #1060) it is versioned, and
    every workflow on it is AUTO_UPGRADE (`print_deployment`): each run moves to the
    current build. Pinned runs were stranded by every deploy (#2066): the Deployment
    rolls with ``Recreate``, so the old build is still current when its pod stops, it
    stops without draining, and nothing serves the runs pinned to it.
    Without (in the API, dev and tests) it is unversioned. Either way a change that
    alters a workflow's commands is made with ``workflow.patched``, so a run started on
    the old code finishes on the new.
    ``tests/test_print_replay.py`` replays committed ``PrintRun`` histories to hold that;
    a new activity name also needs ``patched``, or an old replica takes its task and
    fails it as unregistered.

    ``tests/test_operation_replay.py`` replays committed ``OperationWorkflow`` histories
    the same way.

    Versioned, a run moves to the new build on its next workflow task, so the new build
    replays every open run: a change to a workflow's commands goes behind ``patched``.
    An auto-upgrade run's activities start on the current build, so the old build is
    handed only what its own code scheduled, except an ``Operation``'s kind, which the
    API names: a new API pod starts a kind only the new build has while the old build
    is still current (the API rolls with ``RollingUpdate``). Failed there as
    unregistered, each failure would spend an attempt, and a run-once effect would be
    recorded ``failed`` ("may have been done") without reaching Bambuddy. So
    ``Operation`` defers a kind its worker does not register (#4139): it fails its
    workflow task, which Temporal retries on the current build, and that is told it by
    `RegisteredActivities`. The Deployment may then roll with ``RollingUpdate``. A new
    workflow type is still only safe once no old build is current (README, "Rolling
    it")."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[
            PrintRunWorkflow,
            OperationWorkflow,
            FollowPrint if build_id is None else VersionedFollowPrint,
            # Each LAN printer's report (#3823); AUTO_UPGRADE by the worker's default.
            PrinterWatch,
        ],
        activities=activities,
        workflow_runner=sandboxed_runner(),
        graceful_shutdown_timeout=graceful_shutdown_timeout,
        deployment_config=print_deployment(build_id),
        interceptors=[RegisteredActivities(activities)],
        # An operation's run heartbeats against a 30 s timeout (#4635).
        max_heartbeat_throttle_interval=HEARTBEAT_THROTTLE,
    )


def follow_worker(
    client: Client,
    task_queue: str,
    follow_print: Callable[..., Any],
    *,
    printer_watch: Callable[..., Any] | None = None,
    graceful_shutdown_timeout: timedelta = timedelta(seconds=30),
    build_id: str | None = None,
) -> Worker:
    """``FollowPrint``'s activity on its own queue beside ``task_queue`` (review #1091
    1): each follow holds its slot for as long as the print moves, so it never takes a
    slot from the ``bambuddy`` worker's short activities. Its `FOLLOW_SLOTS` are set
    here, not left to the SDK's default (review #1091 2). An attempt ends at once on a
    shutdown (`FollowActivities`). With ``build_id`` it joins the same deployment
    version as the ``bambuddy`` worker, so the follow's activity is routed within it.
    ``printer_watch`` (#3823) holds a slot per LAN printer for as long as it is
    reachable, beside the follows."""
    return Worker(
        client,
        task_queue=follow_queue(task_queue),
        activities=[follow_print, *([printer_watch] if printer_watch is not None else [])],
        max_concurrent_activities=FOLLOW_SLOTS,
        graceful_shutdown_timeout=graceful_shutdown_timeout,
        deployment_config=print_deployment(build_id),
    )


def print_deployment(build_id: str | None) -> WorkerDeploymentConfig | None:
    """``scadbuddy-print`` at ``build_id``, workflows AUTO_UPGRADE by default (#2066);
    None unversioned."""
    if build_id is None:
        return None
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(deployment_name=PRINT_DEPLOYMENT_NAME, build_id=build_id),
        use_worker_versioning=True,
        default_versioning_behavior=VersioningBehavior.AUTO_UPGRADE,
    )


def projects_deployment(build_id: str) -> WorkerDeploymentConfig:
    """``scadbuddy-projects`` at ``build_id`` (#4146), workflows AUTO_UPGRADE by default,
    as ``scadbuddy-print``'s. Never pinned (plan 2026-10-09 Ruling 13): a flow may run for
    weeks, and its build would have to drain that long. A default rather than a behavior
    on each class, as the library's: ``ProjectWorkflow`` is the harness's ``agent.defn``,
    and the in-process worker, unversioned, could not complete a workflow that declares
    one."""
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(
            deployment_name=PROJECTS_DEPLOYMENT_NAME, build_id=build_id
        ),
        use_worker_versioning=True,
        default_versioning_behavior=VersioningBehavior.AUTO_UPGRADE,
    )


def library_deployment(build_id: str) -> WorkerDeploymentConfig:
    """``scadbuddy-library`` at ``build_id`` (#4090), with no default behavior: the SDK
    then refuses to build the worker unless every workflow on it names its own
    (`housekeeping.library_worker`), so a workflow added to the queue cannot go
    unversioned by default."""
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(deployment_name=LIBRARY_DEPLOYMENT_NAME, build_id=build_id),
        use_worker_versioning=True,
    )


async def make_current(
    client: Client,
    *,
    namespace: str,
    build_id: str,
    deployment_name: str = DEPLOYMENT_NAME,
    conflict_token: bytes = b"",
) -> None:
    """Make `build_id` the deployment's current version: a versioned worker takes new
    workflows only once its version is current. With ``conflict_token`` (from
    :func:`current_routing`), Temporal refuses the write if the deployment changed since
    that read."""
    await client.workflow_service.set_worker_deployment_current_version(
        SetWorkerDeploymentCurrentVersionRequest(
            namespace=namespace,
            deployment_name=deployment_name,
            build_id=build_id,
            conflict_token=conflict_token,
            ignore_missing_task_queues=True,
            allow_no_pollers=True,
        ),
        timeout=RPC_TIMEOUT,
    )


async def current_routing(
    client: Client, *, namespace: str, deployment_name: str = DEPLOYMENT_NAME
) -> tuple[str | None, bytes]:
    """The deployment's current build (None while it has none), and the conflict token
    a write conditional on it carries (#2538)."""
    response = await client.workflow_service.describe_worker_deployment(
        DescribeWorkerDeploymentRequest(namespace=namespace, deployment_name=deployment_name),
        timeout=RPC_TIMEOUT,
    )
    current = response.worker_deployment_info.routing_config.current_deployment_version
    return current.build_id or None, response.conflict_token


async def routes_to(
    client: Client, task_queue: str, *, deployment_name: str, build_id: str
) -> bool:
    """Whether ``task_queue`` itself routes new work to ``build_id`` of
    ``deployment_name``: `DescribeTaskQueue`'s versioning info, what matching routes
    by. A version is current in the deployment a moment before its queues hear of it
    (0.2-0.3 s on the dev server, #4090); until then the previous build still gets the
    queue's new tasks.

    Asked of the workflow-type queue and of the activity-type one: each has its own copy
    of the routing, and an activity is dispatched by the activity type's, which lagged
    the workflow type's on the dev server (#4255). Both answer from their root partition,
    so a non-root partition can lag still: this narrows the window, it does not close it.

    So the queue must have activity pollers of this build: the activity type hears of a
    version only from them. A queue that serves workflows only never answers True, and
    a caller waiting on it waits out its whole timeout: a caller's worker must register
    activities."""
    for kind in (TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY):
        described = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=client.namespace,
                task_queue=TaskQueue(name=task_queue, kind=TaskQueueKind.TASK_QUEUE_KIND_NORMAL),
                task_queue_type=kind,
            ),
            timeout=RPC_TIMEOUT,
        )
        current = described.versioning_info.current_deployment_version
        if current.deployment_name != deployment_name or current.build_id != build_id:
            return False
    return True


def drain_mark(since: datetime) -> Payload:
    """A `DRAINING_MARK` entry's value: ``since`` in seconds since the epoch, as JSON."""
    return Payload(
        metadata={"encoding": b"json/plain"}, data=json.dumps(since.timestamp()).encode()
    )


async def mark_draining(
    client: Client,
    *,
    deployment_name: str,
    build_id: str,
    identities: Collection[str],
    since: datetime,
    prune_within: float,
) -> None:
    """Mark ``identities`` (one worker's) draining since ``since`` in the version's
    metadata (#4948), and remove, in the same write, every other mark older than
    ``prune_within`` seconds: pod churn would otherwise add a key per stopped pod for the
    build's whole life. Every other mark that does not parse goes too, since a reader
    dates it now and so it never grows old. A failed read of the marks only skips that
    pruning. The version exists only once a worker of it has polled: until then Temporal
    refuses the write."""
    stale: list[str] = []
    try:
        marks, unparsable = await _read_marks(
            client, deployment_name=deployment_name, build_id=build_id
        )
    except RPCError:
        logger.warning("could not read this build's drain marks to prune them", exc_info=True)
    else:
        before = datetime.now(UTC) - timedelta(seconds=prune_within)
        stale = [
            f"{DRAINING_MARK}{identity}"
            for identity, marked in marks.items()
            if identity not in identities and (marked < before or identity in unparsable)
        ]
    await client.workflow_service.update_worker_deployment_version_metadata(
        UpdateWorkerDeploymentVersionMetadataRequest(
            namespace=client.namespace,
            deployment_version=DeploymentVersion(
                deployment_name=deployment_name, build_id=build_id
            ),
            upsert_entries={
                f"{DRAINING_MARK}{identity}": drain_mark(since) for identity in identities
            },
            remove_entries=stale,
        ),
        timeout=RPC_TIMEOUT,
    )


async def clear_draining(
    client: Client, *, deployment_name: str, build_id: str, identities: Collection[str]
) -> None:
    """Remove the marks of ``identities``: a container restarted in its pod keeps its
    identity, and its last life's mark must not make a peer skip it (#4948)."""
    await client.workflow_service.update_worker_deployment_version_metadata(
        UpdateWorkerDeploymentVersionMetadataRequest(
            namespace=client.namespace,
            deployment_version=DeploymentVersion(
                deployment_name=deployment_name, build_id=build_id
            ),
            remove_entries=[f"{DRAINING_MARK}{identity}" for identity in identities],
        ),
        timeout=RPC_TIMEOUT,
    )


async def draining_marks(
    client: Client, *, deployment_name: str, build_id: str
) -> dict[str, datetime]:
    """Each marked identity and when its drain began. A mark that does not parse counts
    as new: it is then skipped, and the reader drains in full. That is logged once per
    key and process, since a stopping worker reads the marks on every drain poll."""
    marks, _ = await _read_marks(client, deployment_name=deployment_name, build_id=build_id)
    return marks


async def _read_marks(
    client: Client, *, deployment_name: str, build_id: str
) -> tuple[dict[str, datetime], set[str]]:
    """`draining_marks`, and the identities whose marks do not parse."""
    described = await client.workflow_service.describe_worker_deployment_version(
        DescribeWorkerDeploymentVersionRequest(
            namespace=client.namespace,
            deployment_version=DeploymentVersion(
                deployment_name=deployment_name, build_id=build_id
            ),
        ),
        timeout=RPC_TIMEOUT,
    )
    marks: dict[str, datetime] = {}
    unparsable: set[str] = set()
    for key, value in described.worker_deployment_version_info.metadata.entries.items():
        if not key.startswith(DRAINING_MARK):
            continue
        try:
            since = datetime.fromtimestamp(float(json.loads(value.data)), UTC)
        except (ValueError, TypeError, OverflowError):
            since = datetime.now(UTC)
            unparsable.add(key.removeprefix(DRAINING_MARK))
            if key not in _unparsable_marks:
                _unparsable_marks.add(key)
                logger.warning(
                    "a drain mark does not parse; its worker counts as draining",
                    extra={"identity": key.removeprefix(DRAINING_MARK), "build_id": build_id},
                )
        marks[key.removeprefix(DRAINING_MARK)] = since
    return marks, unparsable


async def polled_elsewhere(
    client: Client,
    task_queue: str,
    *,
    deployment_name: str,
    build_id: str,
    identities: Collection[str],
    marked_within: float,
    within: float = POLLER_SEEN_WITHIN,
) -> bool:
    """Whether a worker of ``build_id`` other than ``identities`` (the asking
    worker's own) polled ``task_queue``'s workflow tasks in the last ``within`` seconds:
    `DescribeTaskQueue`'s pollers, which name each one's identity and build (measured on
    prod, 2026-10-27). A stopping worker leaves its pinned runs to such a worker only
    (#4869): in a rollout the build is still current while the new pods, of the next
    build, start, and none of them serves a run pinned to this one.

    A poller marked draining in the last ``marked_within`` seconds (`mark_draining`) does
    not count (#4948): two pods of one build that stop together would each count the
    other. When the marks cannot be read, the answer is False."""
    described = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=client.namespace,
            task_queue=TaskQueue(name=task_queue),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
        ),
        timeout=RPC_TIMEOUT,
    )
    now = datetime.now(UTC)
    since = now - timedelta(seconds=within)
    others = {
        poller.identity
        for poller in described.pollers
        if poller.deployment_options.deployment_name == deployment_name
        and poller.deployment_options.build_id == build_id
        and poller.identity not in identities
        and poller.last_access_time.ToDatetime(tzinfo=UTC) >= since
    }
    if not others:
        return False
    try:
        marks = await draining_marks(client, deployment_name=deployment_name, build_id=build_id)
    except RPCError:
        logger.warning("could not read this build's drain marks", exc_info=True)
        return False
    marked_since = now - timedelta(seconds=marked_within)
    return any(identity not in marks or marks[identity] < marked_since for identity in others)


async def is_current(
    client: Client, *, namespace: str, build_id: str, deployment_name: str = DEPLOYMENT_NAME
) -> bool:
    """Whether `build_id` is the deployment's current version. Then any worker of this
    build serves the runs pinned to it, so one that stops need not drain (#874)."""
    current, _ = await current_routing(client, namespace=namespace, deployment_name=deployment_name)
    return current == build_id


async def drained(
    client: Client,
    *,
    namespace: str,
    build_id: str,
    deployment_name: str = DEPLOYMENT_NAME,
) -> bool:
    """Whether no workflow pinned to `build_id` is still running. A visibility count,
    not `DescribeWorkerDeploymentVersion`'s drainage status: that one is absent while
    the version is current and still says DRAINING well after its last run has ended."""
    query = (
        f'TemporalWorkerDeploymentVersion="{deployment_name}:{build_id}"'
        ' AND ExecutionStatus="Running"'
    )
    response = await client.workflow_service.count_workflow_executions(
        CountWorkflowExecutionsRequest(namespace=namespace, query=query),
        timeout=RPC_TIMEOUT,
    )
    return response.count == 0


__all__ = [
    "DEPLOYMENT_NAME",
    "LIBRARY_DEPLOYMENT_NAME",
    "PRINT_DEPLOYMENT_NAME",
    "PROJECTS_DEPLOYMENT_NAME",
    "RENDER_TASK_QUEUE_DEFAULT",
    "bambuddy_worker",
    "connect",
    "current_routing",
    "drained",
    "follow_worker",
    "is_current",
    "library_deployment",
    "make_current",
    "projects_deployment",
    "pydantic_data_converter",
    "render_worker",
    "routes_to",
]


#: A run younger than this is left alone: its execution may not have been described yet.
LOST_RUN_GRACE = timedelta(minutes=1)


async def _running(client: Client, workflow_id: str, run_id: str | None) -> bool:
    """Whether that run (or, with no run id, the workflow's latest) is running. Any
    other failure is about Temporal, read as the routes read it (``temporal_failure``):
    ``TemporalUnavailableError`` (or a subclass) when it did not answer within the
    bound, could not answer now or has no such namespace, ``TemporalRefusedError`` when
    it refused. A lazy client's first connect retries for minutes on its own, so the
    call is bounded (review #1316 (10) 1). Only an execution's NOT_FOUND says the run is
    gone: a missing namespace answers NOT_FOUND too, and must never end a live row."""
    try:
        async with asyncio.timeout(DESCRIBE_SECONDS + CONNECT_MARGIN_SECONDS):
            described = await client.get_workflow_handle(workflow_id, run_id=run_id).describe(
                rpc_timeout=timedelta(seconds=DESCRIBE_SECONDS)
            )
    except (TimeoutError, RPCError, RuntimeError) as error:
        if (
            isinstance(error, RPCError)
            and error.status == RPCStatusCode.NOT_FOUND
            and not namespace_not_found(error)
        ):
            return False
        if (failure := temporal_failure(error, workflow_id)) is None:
            raise
        raise failure from error
    return described.status == WorkflowExecutionStatus.RUNNING


async def _end_closed(
    client: Client,
    rows: list[tuple[str, str, str]],
    end: Callable[[str], Awaitable[bool]],
    what: str,
) -> int:
    """End (``end``, true when it ended it) each row whose execution is neither running
    under its run id nor under a later one. One row that cannot be ended is logged and
    left for the next pass (review #1316 (9) 3a). A failure about Temporal stops the
    pass with one log line, since every other row would fail the same way (review #1316
    (11) 2, 3; (12) 1): a warning while it is unavailable, an error once it refused."""
    ended = 0
    for row_id, workflow_id, workflow_run_id in rows:
        try:
            if await _running(client, workflow_id, workflow_run_id) or await _running(
                client, workflow_id, None
            ):
                continue
            if await end(row_id):
                ended += 1
        except TemporalUnavailableError:
            logger.warning(
                "Temporal did not answer; no lost %s is ended this pass", what, exc_info=True
            )
            break
        except TemporalRefusedError:
            logger.error(
                "Temporal refused to describe; no lost %s is ended this pass", what, exc_info=True
            )
            break
        except Exception:
            logger.exception("could not end a lost %s", what, extra={"id": row_id})
    return ended


async def reconcile_lost_runs(
    client: Client, store: PrintRunStore, *, older_than: timedelta = LOST_RUN_GRACE
) -> int:
    """End each ``running`` print run whose execution has closed or is gone (review
    #1061): terminated in the Temporal UI, it never runs ``print_fail``. One still
    running is left to end its row itself, and so is one whose workflow still runs
    under a later run id: a reset continues the same row there. Returns how many it
    ended.

    First, a row a pre-#1052 pod inserted and stopped beating is ended too (review #1061
    (3) 2), as one that may have queued: a stalled pod may yet queue it (review #1316
    2a). Those never had an execution, so they are logged here and not counted (review
    #1316 (11) 4)."""
    upgraded = 0
    # One row that cannot be ended is logged and left for the next pass; the rest are
    # still ended (review #1316 (9) 3a). The pre-#1052 rows need only Postgres, so a
    # Temporal that does not answer never holds them up (review #1316 (10) 1).
    for run_id in await store.stale_pre_1052_runs():
        try:
            if (await store.fail_pre_1052(run_id)).status == "failed":
                upgraded += 1
        except Exception:
            logger.exception("could not end a pre-#1052 print run", extra={"run_id": run_id})
    if upgraded:
        logger.warning("ended print runs a pre-#1052 pod left running", extra={"count": upgraded})

    async def end(run_id: str) -> bool:
        return (await store.fail_lost(run_id)).status == "failed"

    return await _end_closed(client, await store.running_executions(older_than), end, "print run")


async def reconcile_lost_operations(
    client: Client,
    store: OperationStore,
    *,
    where: Mapping[str, str],
    older_than: timedelta = LOST_RUN_GRACE,
) -> int:
    """End each ``running`` operation whose execution has closed or is gone (review
    #1063 1): terminated after ``op_insert``, or past a timeout, it never runs
    ``op_finish``. The same rule as :func:`reconcile_lost_runs`. ``where`` is each
    kind's ``OperationKind.where``, what its problem says to check (#1174). It is
    required, so no caller can forget it; Bambuddy is said only for a row whose kind is
    no longer registered. Returns how many it ended."""
    rows = await store.running_executions(older_than)
    # Each row's own kind, read with it: never parsed back out of the workflow ID.
    kinds = {op_id: kind for op_id, _, _, kind in rows}

    async def end(op_id: str) -> bool:
        lost = operation_lost(where.get(kinds[op_id], "Bambuddy"))
        return (await store.finish(op_id, error=lost)).status == "failed"

    executions = [(op_id, workflow_id, run_id) for op_id, workflow_id, run_id, _ in rows]
    return await _end_closed(client, executions, end, "operation")
