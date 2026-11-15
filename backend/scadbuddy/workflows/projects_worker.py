"""The `projects` worker (spec 2026-10-01 §4.3, plan 2026-10-09 Task B4): flow runs,
and in 6c the flow operations. Never pinned (Ruling 13): a flow may run for weeks, and a
pinned build would hold a drain that long. Versioned AUTO_UPGRADE in its own Deployment
(#4146, `client.projects_deployment`), unversioned in the API's process. Its client must
carry `flows_converter`."""

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import timedelta
from typing import Any

from temporal_agent_harness.harness.code_mode import monty_stepper
from temporalio.client import Client
from temporalio.worker import Worker, WorkerDeploymentConfig
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from scadbuddy.workflows.flow_entries import flow_entry_timeout
from scadbuddy.workflows.flow_tools import FLOW_TOOLS, HOST_FUNCTION_NAMES, run_flow_tool
from scadbuddy.workflows.flows_client import harness_plugins
from scadbuddy.workflows.operation import OperationWorkflow
from scadbuddy.workflows.project import ProjectWorkflow
from scadbuddy.workflows.registered import RegisteredActivities

logger = logging.getLogger(__name__)

#: What `warm_sandbox` runs: it finishes without a host call.
WARM_SCRIPT = "import asyncio\n\n\nasync def main():\n    return 1\n\n\nasyncio.run(main())\n"


def _no_os(name: str, args: tuple[Any, ...]) -> Any:
    raise NotImplementedError(name)


def warm_sandbox() -> None:
    """Run one type-checked script in each of the harness's Monty pools before the worker
    polls (#6346). A pool spawns its first `monty` process on first use, and that
    process's first type check is slow, inside the 1 s the live pool's watchdog
    (`monty_stepper.STEP_TIME_LIMIT_SECS`) gives a step. Measured in the prod projects pod
    on 2026-11-15 with makerworld-import's script: a fresh process's first step took
    0.11 to 0.43 s, a warm one 0.008 s. On a node busy with disk I/O, run
    27da6f4e (the first flow run on its pod, 11 minutes after it started) was stopped with
    "ran for more than 1s without awaiting a host function" before its first host call;
    the same script ran on that pod 7 minutes later.

    A pool keeps one process warm (`pydantic_monty.Monty` `min_processes`, default 1) and
    spawns more, up to the CPU count, only for checkouts at the same time, so this warms
    the one each run's step takes when no other step is running. A warm-up the watchdog
    stopped had its process killed, so it is tried once more on the replacement. Nothing
    here stops the worker's start: a pool that will not warm fails only the steps that
    would have failed anyway. Blocking: run it off the loop."""
    try:
        stubs = run_flow_tool().__code_mode_stubs__
        for _ in range(2):
            step = monty_stepper.ScriptRun(WARM_SCRIPT, stubs, answer_os=_no_os).start()
            if not step.timed_out:
                break
        if step.error:
            logger.warning(
                "the flow sandbox's live pool did not warm (timed out: %s): %s",
                step.timed_out,
                step.error,
            )
        problem = monty_stepper.type_check(WARM_SCRIPT, stubs)
        if problem:
            logger.warning("the flow sandbox's warm-up script did not type-check: %s", problem)
    except Exception:
        logger.warning("the flow sandbox did not warm", exc_info=True)


def projects_runner() -> SandboxedWorkflowRunner:
    """The workflow sandbox, as every worker's (`client.sandboxed_runner`), with the
    Code Mode interpreter's module the process's own: its subprocess pool is shared by
    every run on the worker (plan, "What the pinned harness is")."""
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "opentelemetry", "pydantic_monty"
        )
    )


def projects_worker(
    client: Client,
    task_queue: str,
    activities: Sequence[Callable[..., Any]],
    *,
    workflows: Sequence[type] = (ProjectWorkflow, OperationWorkflow),
    graceful_shutdown_timeout: timedelta = timedelta(seconds=30),
    deployment_config: WorkerDeploymentConfig | None = None,
    host_functions: Iterable[str] = HOST_FUNCTION_NAMES,
) -> Worker:
    """The `projects` queue's worker. `flow_entry_timeout` (Ruling 11) is always added
    here, so a caller never passes it: a name registered twice fails the worker's start.

    Its workflows are told what it serves (`RegisteredActivities`, #4139, #4146): an
    ``Operation`` of a ``flow_*`` kind it lacks, and a run whose start names a host
    function it lacks (``host_functions``, this build's unless a test says otherwise),
    wait for a build that has them instead of failing here. With ``deployment_config``
    (`client.projects_deployment`) it is versioned."""
    all_activities = [*activities, flow_entry_timeout]
    return Worker(
        client,
        task_queue=task_queue,
        workflows=list(workflows),
        activities=all_activities,
        plugins=harness_plugins(FLOW_TOOLS),
        workflow_runner=projects_runner(),
        graceful_shutdown_timeout=graceful_shutdown_timeout,
        deployment_config=deployment_config,
        interceptors=[RegisteredActivities(all_activities, host_functions)],
    )
