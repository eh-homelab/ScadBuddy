# Unified agent runtime, Phase 11: the design check (Task 11.1)

Plan: [`2026-10-13-unified-agent-runtime.md`](2026-10-13-unified-agent-runtime.md), "Phase 11:
Versioned queues, then `RollingUpdate` (D6)". Epic eh-homelab/ScadBuddy#2726. Written
2026-11-01.

## 1. The question

Task 11.2 puts the shared `agent-tools` worker into the Worker Deployment `scadbuddy-agent`,
beside `agent-sessions` (versioned since Task 5.1). Two kinds of workflow schedule activities
on `agent-tools`:

- versioned ones: `AgentSession` (`agent-sessions`, `scadbuddy-agent`, `AUTO_UPGRADE`), and the
  workflows on `agent-tools` itself (`AgentOperation`, `ToolCall`, `BridgeCall`,
  `BackgroundTask`), which become versions of `scadbuddy-agent` with it;
- workflows outside that deployment: the backend's `ProjectWorkflow` on `projects`. In prod it
  is `AUTO_UPGRADE` in a deployment of its own, `scadbuddy-projects` (#4146); in-process
  (`SCADBUDDY_TEMPORAL_PROJECTS_WORKER_INPROCESS`) it is unversioned. Its tier checks
  (`flow_tool_tier`, `turn_tool_tier`) and its calls (`<tool>`, `turn:<server>__<tool>`) are
  activities on `agent-tools` (`backend/scadbuddy/workflows/flow_tools.py`).

The gate fails if such a caller's activity can reach an old-build worker that lacks it, with no
documented setting to prevent that.

## 2. What Temporal documents

- **Activities follow their workflow's version, unless they are independent.**
  "For Auto-Upgrade Workflows, an Activity starts on the Target Worker Deployment Version of
  the Workflow." Independent Activities, those on a task queue that is not a member of the
  calling workflow's version, "start on the Current or Ramping Version of their own Task
  Queue independently from their Workflow". One of the listed cases is an activity "in a
  separate Worker Deployment that has its own Workflows, but other Workflows" reuse it from
  other Worker Deployments: `ProjectWorkflow` calling `agent-tools`.
  ([Worker Versioning, "Versioning behaviors"](https://docs.temporal.io/worker-versioning#versioning-behaviors))
- **The Current Version** takes 100% of new and Auto-Upgrade workflows without a Ramping
  Version, and "if no current version is specified, the default is unversioned"
  ([Worker Versioning, "Versioning Definitions"](https://docs.temporal.io/worker-versioning#versioning-definitions)).
- **Moving from unversioned to versioned:** once a deployment has a current version, open
  unversioned workflows move to it at their next task
  ([migration guide](https://docs.temporal.io/production-deployment/worker-deployments/unversioned-to-versioned-migration)).
- **`SetWorkerDeploymentCurrentVersion`'s `ignore_missing_task_queues`:** without it, the call
  is refused unless the new version polls every queue the current version ever polled,
  "with the following exclusions: Task Queues that are not used anymore (inferred by having
  empty backlog and a task add_rate of 0.)"
  ([`request_response.proto`](https://github.com/temporalio/api/blob/master/temporal/api/workflowservice/v1/request_response.proto),
  `SetWorkerDeploymentCurrentVersionRequest`).
- The docs do not say where an unversioned workflow's activity on a versioned queue goes, nor
  what a queue that is a member of an old version only does once a newer version is current.
  §3 measures both.

## 3. The measurement

Where: `agent/test/temporal.versioning.temporal.test.ts`, first test, on the Temporal CLI 1.9.1
dev server (Server 1.32.0; prod runs 1.32.1, `kubectl --context primary -n bambuddy get deploy
scadbuddy-temporal-v2-matching`). How: one deployment `agent-<id>` with an old build `v1` and a
new build `v2`, each with a versioned `AUTO_UPGRADE` worker on a caller queue (as
`agent-sessions`) and on a shared activity queue (as `agent-tools`, `a_new` registered only on
`v2`); an unversioned caller queue (an in-process `ProjectWorkflow`); a caller queue in another
deployment `projects-<id>`, `AUTO_UPGRADE` (prod's `ProjectWorkflow`). Each row is 8 workflows
per caller, each running the activity once with no retry. All of it was also run first as a
throwaway script with the same steps, with the same results.

| Step | `AgentSession`-like caller | unversioned caller | other-deployment caller |
| --- | --- | --- | --- |
| `v1` current, `v1` polls; `whoami` | 8 on `v1` | 8 on `v1` | 8 on `v1` |
| `v1` current, `v1` and `v2` poll; `a_new` | 8 `NotFoundError` on `v1` | 8 `NotFoundError` on `v1` | 8 `NotFoundError` on `v1` |
| `v2` made current, at once; `a_new` | 8 on `v2` | 8 on `v2` | 8 on `v2` |
| `v2` current, 10 s later; `a_new`, `whoami` | 8 on `v2` | 8 on `v2` | 8 on `v2` |
| old replica's own queue, versioned in `v1`, after `v2` is current | ScheduleToStart timeout | ScheduleToStart timeout | (not run) |
| old replica's own queue, unversioned, after `v2` is current | 2 on its poller | 2 on its poller | 2 on its poller |
| rollback: `v0` current, its shared-queue worker unversioned | 4 on `v0` | 4 on `v0` | 4 on `v0` |

A workflow open across the switch (started on `v1`, signalled after `v2` was current) ran its
next activity on `v2`. The deployment then lists `v2` current and `v1` draining.

Findings:

1. **Every kind of caller routes alike: to the deployment's current build.** An unversioned
   caller and another deployment's caller follow the Current Version of `agent-tools`'
   deployment exactly as `AgentSession` does. Once the new build is current, no caller's task
   reaches the old build, though it still polls.
2. **An old build gets a new activity only while it is still current**, and then from every
   kind of caller alike. The documented control is the Current Version itself: make the new
   build current before anything depends on its new names. Two windows stay:
   - before the switch, a caller of another build (the backend, the API's `agent` container)
     can name an activity the current agent build lacks. `ProjectWorkflow` asks the tier of
     every call on `agent-tools` first, so the current build answers it: a name it does not
     know fails there as a non-retryable `ToolError`, which the script sees
     (`agent/src/temporal/flowTools.ts`), and the call itself is never scheduled. The prod `agent-tools` worker also
     registers a `default` activity (`turnToolActivity`), which fails any other unknown name
     as a **retryable** `NotFoundError` (`agent/src/temporal/turnTools.ts`), so a retry after
     the switch lands on the new build;
   - right after the switch a non-root partition of the activity queue can lag its root and
     still hand a task to the old build (#4255: 0.3 s after the switch, measured on the
     backend's `library` queue in CI). That window is the same for a versioned caller.

   Unversioned today, any of the replicas takes any task during the whole rollout, so both
   windows are strictly smaller than now. **The gate passes.**
3. **The host queues must stay unversioned.** A queue only one replica polls
   (`agent-tools.<hostname>`, `agent-sessions.<hostname>`) is a member of that replica's build
   only. Versioned, once a newer build is current its tasks reach no one (the ScheduleToStart
   row): a turn still running on an old replica during its drain, or a pinned call for it,
   would wait out its timeout. Unversioned, its own poller keeps taking them from every kind
   of caller. Plan D6 and Task 11.2 said to version the host queues; this changes that.
4. **Temporal's own missing-queue check does not cover a quiet queue.** With
   `ignore_missing_task_queues` off, Temporal still made `v2` current while it polled only
   the caller queue, because the shared queue had had no tasks (second test, before the fix).
   The shared queue would then route to unversioned pollers, of which a versioned deployment
   has none. So Task 11.2 makes a build current only once
   `DescribeWorkerDeploymentVersion` lists it on both of its queues.
5. **A rollback past Phase 11 needs nothing:** a queue the current build does not poll goes to
   its unversioned pollers (the last row), so the old image's unversioned `agent-tools` worker
   takes its tasks again.

## 4. What follows for Tasks 11.2–11.4

- **11.2** (agent): `agent-tools` joins `scadbuddy-agent` with the same build id as
  `agent-sessions`, every workflow `AUTO_UPGRADE` (the worker's default behavior, so
  `AgentOperation` needs no annotation of its own; its changes already go behind `patched()`).
  The build is made current once per process, never at a reconnect, and only once Temporal
  lists it on both queues (finding 4). `/readyz` waits until it is current, so a rolling update
  stops an old pod only after the switch. The host queues stay unversioned (finding 3).
- **11.3** (backend `library`): already done by #4090 (`client.library_deployment`,
  `main._make_library_current`, `backend/tests/test_library_versioning.py`), including the
  partition-lag note of #4255. Nothing is left.
- **11.4** (clusters): the `scadbuddy` Deployment is `RollingUpdate` with `maxSurge: 1,
  maxUnavailable: 0`, and stays so; Task 11.4's `maxUnavailable: 1, maxSurge: 0` would take a
  replica away during every deploy, and the cluster has room for the surge pod (10 schedulable
  amd64 workers against the 4 that 3 replicas plus 1 surge need under the required
  anti-affinity). Its `terminationGracePeriodSeconds` goes from the default 30 to 60: the
  backend's `library` worker may take up to 50 s to stop (`LIBRARY_WORKER_STOP_TIMEOUT` in
  `backend/scadbuddy/main.py`: a 30 s graceful shutdown, a 10 s sweep join, 10 s of margin),
  and the `agent` container's worker 15 s (`shutdownForceTime`). The turns' own stop (12 s
  `stopTurns` drain, 15 s force) runs on `scadbuddy-agent-worker`, which already has 60 s.
