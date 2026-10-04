from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from temporalio.api.workflowservice.v1 import (
    CountWorkflowExecutionsRequest,
    DescribeWorkerDeploymentRequest,
    SetWorkerDeploymentCurrentVersionRequest,
)
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import VersioningBehavior
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker, WorkerDeploymentConfig, WorkerDeploymentVersion
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from scadbuddy.bambuddy.follow import FOLLOW_SLOTS
from scadbuddy.bambuddy.runs import PrintRunStore
from scadbuddy.operations.store import OperationStore
from scadbuddy.workflows.activities import RenderActivities
from scadbuddy.workflows.follow import FollowPrint, VersionedFollowPrint, follow_queue
from scadbuddy.workflows.operation import OperationWorkflow
from scadbuddy.workflows.pipelines import RenderPiece, RenderPreview, TemplatePipeline
from scadbuddy.workflows.printing import PrintRunWorkflow
from scadbuddy.workflows.problems import OPERATION_LOST

RENDER_TASK_QUEUE_DEFAULT = "render"
DEPLOYMENT_NAME = "scadbuddy-render"
#: The print worker's deployment (#1060, spec 2026-10-01 §5.5): `--queue bambuddy`.
PRINT_DEPLOYMENT_NAME = "scadbuddy-print"
RPC_TIMEOUT = timedelta(seconds=10)


async def connect(address: str, namespace: str, *, lazy: bool = False) -> Client:
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


def render_worker(
    client: Client,
    task_queue: str,
    activities: RenderActivities,
    *,
    build_id: str,
    max_concurrent_activities: int,
    graceful_shutdown_timeout: timedelta = timedelta(),
) -> Worker:
    """The render worker (spec §3.5): versioned by build id so a rolling deploy lets
    old workers drain the workflows they started."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[TemplatePipeline, RenderPiece, RenderPreview],
        activities=activities.all(),
        # The interceptor's workflow spans run inside the sandbox; OpenTelemetry's
        # module state must be the process's, not a sandboxed copy.
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules("opentelemetry")
        ),
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
    With ``build_id`` (the ``scadbuddy-print`` Deployment, #1060) it is versioned like
    the render worker: ``PrintRun`` and ``Operation`` are pinned to the build that started
    them, and ``FollowPrint`` is AUTO_UPGRADE (`VersionedFollowPrint`), since it lasts as long
    as the print.
    Without (in the API, dev and tests) it is unversioned. Either way a change that
    alters a workflow's commands is made with ``workflow.patched``, so a run started on
    the old code finishes on the new.
    ``tests/test_print_replay.py`` replays committed ``PrintRun`` histories to hold that;
    a new activity name also needs ``patched``, or an old replica takes its task and
    fails it as unregistered.

    A new workflow type, or activity names no ``patched`` can guard, cannot be rolled:
    the release that adds them needs a ``Recreate`` rollout (or the old replicas scaled
    to 0 first). #1053 is one: it adds ``Operation`` and its ``op_*`` and
    ``op.<kind>.*`` activities, and ``FollowPrint`` with its ``follow_print`` activity
    on the follow queue (README, "Bambuddy writes on the ``bambuddy`` queue")."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[
            PrintRunWorkflow,
            OperationWorkflow,
            FollowPrint if build_id is None else VersionedFollowPrint,
        ],
        activities=activities,
        graceful_shutdown_timeout=graceful_shutdown_timeout,
        deployment_config=print_deployment(build_id),
    )


def follow_worker(
    client: Client,
    task_queue: str,
    follow_print: Callable[..., Any],
    *,
    graceful_shutdown_timeout: timedelta = timedelta(seconds=30),
    build_id: str | None = None,
) -> Worker:
    """``FollowPrint``'s activity on its own queue beside ``task_queue`` (review #1091
    1): each follow holds its slot for as long as the print moves, so it never takes a
    slot from the ``bambuddy`` worker's short activities. Its `FOLLOW_SLOTS` are set
    here, not left to the SDK's default (review #1091 2). An attempt ends at once on a
    shutdown (`FollowActivities`). With ``build_id`` it joins the same deployment
    version as the ``bambuddy`` worker, so the follow's activity is routed within it."""
    return Worker(
        client,
        task_queue=follow_queue(task_queue),
        activities=[follow_print],
        max_concurrent_activities=FOLLOW_SLOTS,
        graceful_shutdown_timeout=graceful_shutdown_timeout,
        deployment_config=print_deployment(build_id),
    )


def print_deployment(build_id: str | None) -> WorkerDeploymentConfig | None:
    """``scadbuddy-print`` at ``build_id``, workflows pinned by default; None unversioned."""
    if build_id is None:
        return None
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(deployment_name=PRINT_DEPLOYMENT_NAME, build_id=build_id),
        use_worker_versioning=True,
        default_versioning_behavior=VersioningBehavior.PINNED,
    )


async def make_current(
    client: Client, *, namespace: str, build_id: str, deployment_name: str = DEPLOYMENT_NAME
) -> None:
    """Make `build_id` the deployment's current version: a versioned worker takes new
    workflows only once its version is current."""
    await client.workflow_service.set_worker_deployment_current_version(
        SetWorkerDeploymentCurrentVersionRequest(
            namespace=namespace,
            deployment_name=deployment_name,
            build_id=build_id,
            ignore_missing_task_queues=True,
            allow_no_pollers=True,
        ),
        timeout=RPC_TIMEOUT,
    )


async def is_current(
    client: Client, *, namespace: str, build_id: str, deployment_name: str = DEPLOYMENT_NAME
) -> bool:
    """Whether `build_id` is the deployment's current version. Then any worker of this
    build serves the runs pinned to it, so one that stops need not drain (#874)."""
    response = await client.workflow_service.describe_worker_deployment(
        DescribeWorkerDeploymentRequest(namespace=namespace, deployment_name=deployment_name),
        timeout=RPC_TIMEOUT,
    )
    current = response.worker_deployment_info.routing_config.current_deployment_version
    return current.build_id == build_id


async def drained(
    client: Client,
    *,
    namespace: str,
    build_id: str,
    deployment_name: str = DEPLOYMENT_NAME,
    ignore_types: Sequence[str] = (),
) -> bool:
    """Whether no workflow pinned to `build_id` is still running. A visibility count,
    not `DescribeWorkerDeploymentVersion`'s drainage status: that one is absent while
    the version is current and still says DRAINING well after its last run has ended.
    ``ignore_types`` are AUTO_UPGRADE types (``FollowPrint``): attributed to the build
    until their next workflow task, they move to the current one rather than wait."""
    query = (
        f'TemporalWorkerDeploymentVersion="{deployment_name}:{build_id}"'
        ' AND ExecutionStatus="Running"'
    )
    for name in ignore_types:
        query += f' AND WorkflowType!="{name}"'
    response = await client.workflow_service.count_workflow_executions(
        CountWorkflowExecutionsRequest(namespace=namespace, query=query),
        timeout=RPC_TIMEOUT,
    )
    return response.count == 0


__all__ = [
    "DEPLOYMENT_NAME",
    "PRINT_DEPLOYMENT_NAME",
    "RENDER_TASK_QUEUE_DEFAULT",
    "bambuddy_worker",
    "connect",
    "drained",
    "follow_worker",
    "is_current",
    "make_current",
    "pydantic_data_converter",
    "render_worker",
]


#: A run younger than this is left alone: its execution may not have been described yet.
LOST_RUN_GRACE = timedelta(minutes=1)


async def _running(client: Client, workflow_id: str, run_id: str | None) -> bool:
    """Whether that run (or, with no run id, the workflow's latest) is running."""
    try:
        described = await client.get_workflow_handle(workflow_id, run_id=run_id).describe()
    except RPCError as error:
        if error.status != RPCStatusCode.NOT_FOUND:
            raise
        return False
    return described.status == WorkflowExecutionStatus.RUNNING


async def reconcile_lost_runs(
    client: Client, store: PrintRunStore, *, older_than: timedelta = LOST_RUN_GRACE
) -> int:
    """End each ``running`` print run whose execution has closed or is gone (review
    #1061): terminated in the Temporal UI, it never runs ``print_fail``. One still
    running is left to end its row itself, and so is one whose workflow still runs
    under a later run id: a reset continues the same row there. Returns how many it
    ended."""
    ended = 0
    for run_id, workflow_id, workflow_run_id in await store.running_executions(older_than):
        if await _running(client, workflow_id, workflow_run_id) or await _running(
            client, workflow_id, None
        ):
            continue
        if (await store.fail_lost(run_id)).status == "failed":
            ended += 1
    return ended


async def reconcile_lost_operations(
    client: Client, store: OperationStore, *, older_than: timedelta = LOST_RUN_GRACE
) -> int:
    """End each ``running`` operation whose execution has closed or is gone (review
    #1063 1): terminated after ``op_insert``, or past a timeout, it never runs
    ``op_finish``. The same rule as :func:`reconcile_lost_runs`. Returns how many it
    ended."""
    ended = 0
    for op_id, workflow_id, workflow_run_id in await store.running_executions(older_than):
        if await _running(client, workflow_id, workflow_run_id) or await _running(
            client, workflow_id, None
        ):
            continue
        if (await store.finish(op_id, error=OPERATION_LOST)).status == "failed":
            ended += 1
    return ended
