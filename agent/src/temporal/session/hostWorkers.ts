import type { DataConverter } from '@temporalio/common'
import type { WorkerOptions } from '@temporalio/worker'
import { TASK_QUEUE } from '../names.js'
import { AgentWorker, workerIdentity } from '../worker.js'
import { sessionHostQueue } from './names.js'

// A replica's own queues (spec 2026-10-13 §5.2), two workers that stop together:
//
//   agent-tools.<identity>     DurableSession's pinned calls (#2320, #2581): http_request, the
//                              browser and kept plugin clients, and the `turn_tools` listing,
//                              which gets 5 s to start and one attempt; and a classic or
//                              unified session's Monitor commands (`bg_monitor_command`, which
//                              run where its turns' Bash runs). The SDK's default limit.
//                              Also the replica's own BridgeCall workflows (#5862): a browser
//                              call to a tab on another replica runs its workflow tasks here,
//                              where nothing waits on a versioned queue's routing.
//   agent-sessions.<identity>  `session_turn` and `release_worker` only, limited to the largest
//                              warm cache the setting allows plus two for releases (main.ts
//                              SESSION_HOST_SLOTS), with the turns' shutdown grace.
//
// Apart, because a turn holds its slot for the whole turn: on one queue with one limit, the
// replica's turns would starve the pinned calls (10 slots where they have 100 today), and pinned
// calls filling the queue would let a turn miss its 5 s schedule-to-start, which the workflow
// reads as a dead home. Both are unversioned, so no workflow is pinned to a build there and a drain
// has nothing to wait for. The only workflows on them are this replica's own BridgeCalls, which
// last seconds; one still running when the pod goes ends on its execution timeout.

type Activities = NonNullable<WorkerOptions['activities']>

/**
 * The pinned worker's sticky cache when it runs BridgeCall (#5862 review): each lasts seconds, and a
 * replica has a few in flight at once, so the SDK's memory-sized default (thousands) is never needed
 * on an `api` pod. One evicted early is replayed from its short history.
 */
export const BRIDGE_CACHED_WORKFLOWS = 50

/**
 * The pinned worker's shutdown grace and force times (#5881 review): its stop starts once the turns
 * drain, so both must end inside shutdown()'s 10 s deadline with a margin. A Monitor command still
 * running at 6 s is cancelled, and at 8 s the worker stops regardless, before the pool closes.
 */
export const PINNED_SHUTDOWN_GRACE_MS = 6_000
export const PINNED_SHUTDOWN_FORCE_MS = 8_000
/** How long after its grace the pinned worker stops regardless: a cancelled activity's window to end. */
export const PINNED_SHUTDOWN_FORCE_MARGIN_MS = PINNED_SHUTDOWN_FORCE_MS - PINNED_SHUTDOWN_GRACE_MS

export type HostWorkersOptions = {
  address: string
  namespace: string
  /** The replica's identity (plugins/pluginSessions.ts `replicaIdentity`). */
  identity: string
  dataConverter?: DataConverter
  /** The pinned calls' activities, on `agent-tools.<identity>`. */
  pinned: Activities
  /** The workflows `agent-tools.<identity>` runs: BridgeCall (main.ts, `bridge-bundle.js`; #5862). */
  pinnedWorkflows?: Pick<WorkerOptions, 'workflowBundle' | 'workflowsPath'>
  /**
   * `session_turn` and `release_worker`, on `agent-sessions.<identity>`, how many may run at once,
   * and its own shutdown grace: a turn's (TurnHost.stop), longer than the pinned calls'.
   */
  sessions?: { activities: Activities; slots: number; shutdownGraceMs?: number; shutdownForceMs?: number }
  shutdownGraceMs?: number
}

export type HostWorkers = { pinned: AgentWorker; sessions: AgentWorker | undefined; stop(): Promise<void> }

/**
 * Stops `worker` only once `drained` settles (#5862 review): the pinned worker runs this replica's
 * own BridgeCalls, whose workflow tasks a draining turn's browser_* call needs until the turn ends.
 * Its promise is the stop's, for a shutdown to await before the database closes.
 */
export function stopAfter(worker: { stop(): Promise<void> }, drained: Promise<unknown>): Promise<void> {
  // Never rejects: a shutdown that fails before awaiting it must not leave an unhandled rejection.
  return drained
    .catch(() => undefined)
    .then(() => worker.stop())
    .catch((err: unknown) => console.error('agent: the pinned host worker did not stop cleanly', err instanceof Error ? err.name : 'error'))
}

export function startHostWorkers(options: HostWorkersOptions): HostWorkers {
  const common = {
    address: options.address,
    namespace: options.namespace,
    // Polled as this replica, so a poller listing names it (affinity.ts `liveHostQueues`).
    identity: workerIdentity(options.identity),
    ...(options.dataConverter ? { dataConverter: options.dataConverter } : {}),
    ...(options.shutdownGraceMs !== undefined ? { shutdownGraceMs: options.shutdownGraceMs } : {}),
  }
  const pinned = AgentWorker.start({
    ...common,
    taskQueue: `${TASK_QUEUE}.${options.identity}`,
    activities: options.pinned,
    shutdownGraceMs: options.shutdownGraceMs ?? PINNED_SHUTDOWN_GRACE_MS,
    // Force keeps its margin after any grace, so a cancelled activity always has a window to end.
    shutdownForceMs: (options.shutdownGraceMs ?? PINNED_SHUTDOWN_GRACE_MS) + PINNED_SHUTDOWN_FORCE_MARGIN_MS,
    ...(options.pinnedWorkflows ? { workflows: options.pinnedWorkflows, maxCachedWorkflows: BRIDGE_CACHED_WORKFLOWS } : {}),
  })
  const sessions = options.sessions
    ? AgentWorker.start({
        ...common,
        taskQueue: sessionHostQueue(options.identity),
        activities: options.sessions.activities,
        maxConcurrentActivityTaskExecutions: options.sessions.slots,
        ...(options.sessions.shutdownGraceMs !== undefined ? { shutdownGraceMs: options.sessions.shutdownGraceMs } : {}),
        ...(options.sessions.shutdownForceMs !== undefined ? { shutdownForceMs: options.sessions.shutdownForceMs } : {}),
      })
    : undefined
  return {
    pinned,
    sessions,
    stop: async () => {
      await Promise.all([pinned.stop(), sessions?.stop()])
    },
  }
}
