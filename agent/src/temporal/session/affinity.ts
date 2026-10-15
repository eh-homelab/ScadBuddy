import type { Client } from '@temporalio/client'
import { ApplicationFailure } from '@temporalio/common'
import { seenAtMs } from '../../sessions/durable.js'
import type { WarmSessions } from '../../sessions/warm.js'
import { TASK_QUEUE } from '../names.js'
import { replicaOfIdentity } from '../worker.js'
import type { AcquireInput } from './models.js'
import { ACQUIRE_WORKER_ACTIVITY, NO_CAPACITY, RELEASE_WORKER_ACTIVITY, sessionHostQueue } from './names.js'

// Session affinity (spec 2026-10-13 §5.2), Go's Worker Sessions built from worker-specific
// task queues (https://docs.temporal.io/guides/worker-execution-affinity):
//
//   workflow.CreateSession   -> acquire_worker, on the shared `agent-tools` (any replica)
//   the session's own queue  -> the answering replica's `agent-sessions.<identity>` (`home`)
//   workflow.CompleteSession -> release_worker, on `home`
//
// acquire_worker reserves a place in the answering replica's warm cache (warm.ts `reserve`),
// which evicts that replica's least recently used idle process when it is full, and refuses
// with the retryable NoCapacity only when every process there runs a turn, so its retry
// may land on another replica.
//
// A replica the turn was lost on (`exclude`) is a preference, not a ban. Asked, it answers a
// referral: itself as home, and `elsewhere`, the other live replicas that could take the turn
// (`liveHosts`), which the workflow asks first, each on its own session host queue, where the
// same activity reserves a place; only when none answers is the lost home the home again. A
// refusal would do instead only if its retry were sure to reach another replica, and it is
// not: the shared queue hands a retry to whichever poller is next, which can be the same
// replica ten times over. A ban would never lift where the lost home is the only one there is:
// a replica restarted in place keeps its hostname, so its queue name, and a lone replica whose
// heartbeat stalled is the only poller its own turn can go to.

export type Acquired = {
  hostQueue: string
  identity: string
  /** From a replica the turn was lost on: the session host queues to ask first (`acquire_worker` there), this one only when none answers. */
  elsewhere?: string[]
}
export type ReleaseInput = { sessionId: string }

export type AcquireDeps = {
  warm: Pick<WarmSessions, 'reserve'>
  hostQueue: string
  identity: string
  /** The session host queues a turn can be placed on now, this replica's among them (`liveHostQueues`); read only for an excluded replica. */
  liveHosts: () => Promise<ReadonlySet<string>>
}

/** `acquire_worker`, for the shared `agent-tools` worker, and for the replica's session host worker when another replica refers the turn here. */
export function acquireActivities(deps: AcquireDeps) {
  return {
    [ACQUIRE_WORKER_ACTIVITY]: async ({ sessionId, exclude }: AcquireInput): Promise<Acquired> => {
      if (exclude?.includes(deps.hostQueue)) {
        // The turn was lost here already (a stalled heartbeat, a shutdown): the replicas that
        // could take it instead, asked first; no place is reserved here for a turn that is leaving.
        const elsewhere = [...(await deps.liveHosts())].filter((queue) => queue !== deps.hostQueue && !exclude.includes(queue))
        if (elsewhere.length > 0) return { hostQueue: deps.hostQueue, identity: deps.identity, elsewhere }
      }
      if (deps.warm.reserve(sessionId) === 'full') {
        throw ApplicationFailure.retryable(`every warm session on ${deps.identity} is running a turn`, NO_CAPACITY)
      }
      return { hostQueue: deps.hostQueue, identity: deps.identity }
    },
  }
}

/** `release_worker`, for the replica's session host worker only: frees the session's place and process here. */
export function releaseActivities(warm: Pick<WarmSessions, 'release'>) {
  return {
    [RELEASE_WORKER_ACTIVITY]: async ({ sessionId }: ReleaseInput): Promise<void> => {
      await warm.release(sessionId)
    },
  }
}

/** A poller seen longer ago than this has gone (sessions/durable.ts POLLER_FRESH_MS: a live worker's long poll is renewed within a minute). */
export const HOST_FRESH_MS = 70_000
const DESCRIBE_DEADLINE_MS = 10_000
const ACTIVITY_TASK_QUEUE = 2

export type HostLister = Pick<Client, 'workflowService' | 'options' | 'withDeadline'>

/**
 * The session host queues a turn can be placed on now, from DescribeTaskQueue: each replica
 * whose `agent-tools` worker polls (a poller's identity names it, temporal/worker.ts
 * `workerIdentity`), whose own `agent-sessions.<replica>` is polled too, both within
 * `freshMs`. A replica that does not host sessions polls the first and not the second.
 */
export async function liveHostQueues(client: HostLister, freshMs = HOST_FRESH_MS): Promise<Set<string>> {
  const now = Date.now()
  const fresh = async (queue: string): Promise<string[]> => {
    const { pollers } = await client.withDeadline(now + DESCRIBE_DEADLINE_MS, () =>
      client.workflowService.describeTaskQueue({
        namespace: client.options.namespace,
        taskQueue: { name: queue },
        taskQueueType: ACTIVITY_TASK_QUEUE,
      }),
    )
    return (pollers ?? []).flatMap((p) => (p.identity && now - seenAtMs(p.lastAccessTime) < freshMs ? [p.identity] : []))
  }
  const replicas = new Set((await fresh(TASK_QUEUE)).map(replicaOfIdentity))
  const live = new Set<string>()
  for (const replica of replicas) {
    const queue = sessionHostQueue(replica)
    if ((await fresh(queue)).length > 0) live.add(queue)
  }
  return live
}
