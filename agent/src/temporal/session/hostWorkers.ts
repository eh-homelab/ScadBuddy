import type { DataConverter } from '@temporalio/common'
import type { WorkerOptions } from '@temporalio/worker'
import { TASK_QUEUE } from '../names.js'
import { AgentWorker } from '../worker.js'
import { sessionHostQueue } from './names.js'

// A replica's own queues (spec 2026-10-13 §5.2), two workers that stop together:
//
//   agent-tools.<identity>     DurableSession's pinned calls (#2320, #2581): http_request, the
//                              browser and kept plugin clients, and the `turn_tools` listing,
//                              which gets 5 s to start and one attempt. The SDK's default limit.
//   agent-sessions.<identity>  `session_turn` and `release_worker` only, limited to the warm
//                              cache's slots plus two for releases (`warm_max_per_replica + 2`).
//
// Apart, because a turn holds its slot for the whole turn: on one queue with one limit, the
// replica's turns would starve the pinned calls (10 slots where they have 100 today), and pinned
// calls filling the queue would let a turn miss its 5 s schedule-to-start, which the workflow
// reads as a dead home. Both are unversioned, as activity-only queues: no workflow is pinned to
// them, so a drain has nothing to wait for.

type Activities = NonNullable<WorkerOptions['activities']>

export type HostWorkersOptions = {
  address: string
  namespace: string
  /** The replica's identity (plugins/pluginSessions.ts `replicaIdentity`). */
  identity: string
  dataConverter?: DataConverter
  /** The pinned calls' activities, on `agent-tools.<identity>`. */
  pinned: Activities
  /** `session_turn` and `release_worker`, on `agent-sessions.<identity>`, and how many may run at once. */
  sessions?: { activities: Activities; slots: number }
  shutdownGraceMs?: number
}

export type HostWorkers = { pinned: AgentWorker; sessions: AgentWorker | undefined; stop(): Promise<void> }

export function startHostWorkers(options: HostWorkersOptions): HostWorkers {
  const common = {
    address: options.address,
    namespace: options.namespace,
    ...(options.dataConverter ? { dataConverter: options.dataConverter } : {}),
    ...(options.shutdownGraceMs !== undefined ? { shutdownGraceMs: options.shutdownGraceMs } : {}),
  }
  const pinned = AgentWorker.start({ ...common, taskQueue: `${TASK_QUEUE}.${options.identity}`, activities: options.pinned })
  const sessions = options.sessions
    ? AgentWorker.start({
        ...common,
        taskQueue: sessionHostQueue(options.identity),
        activities: options.sessions.activities,
        maxConcurrentActivityTaskExecutions: options.sessions.slots,
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
