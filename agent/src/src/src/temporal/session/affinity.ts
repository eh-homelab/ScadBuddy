import { ApplicationFailure } from '@temporalio/common'
import type { WarmSessions } from '../../sessions/warm.js'
import type { AcquireInput } from './models.js'
import { ACQUIRE_WORKER_ACTIVITY, NO_CAPACITY, RELEASE_WORKER_ACTIVITY } from './names.js'

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
// may land on another replica. A replica the turn was lost on (`exclude`) refuses it the same way.

export type Acquired = { hostQueue: string; identity: string }
export type ReleaseInput = { sessionId: string }

/** `acquire_worker`, for the shared `agent-tools` worker: this replica's place and queue. */
export function acquireActivities(deps: { warm: Pick<WarmSessions, 'reserve'>; hostQueue: string; identity: string }) {
  return {
    [ACQUIRE_WORKER_ACTIVITY]: async ({ sessionId, exclude }: AcquireInput): Promise<Acquired> => {
      // The turn was lost here already (a stalled heartbeat, a shutdown): its retry goes to another replica.
      if (exclude?.includes(deps.hostQueue)) {
        throw ApplicationFailure.retryable(`session ${sessionId}'s turn was lost on ${deps.identity}`, NO_CAPACITY)
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
