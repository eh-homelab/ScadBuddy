import { Context } from '@temporalio/activity'
import { ApplicationFailure } from '@temporalio/common'
import type { TurnBeat, TurnHost } from '../../sessions/turnHost.js'
import type { SessionTurnInput, SessionTurnResult } from './models.js'
import { SESSION_TURN_ACTIVITY, SHUTTING_DOWN_FAILURE } from './names.js'

// `session_turn` (spec 2026-10-13 §5.2), on a replica's session host queue only: TurnHost.run
// under the activity's heartbeat. It heartbeats every HEARTBEAT_MS (the workflow's
// heartbeatTimeout is 20 s, so a replica that dies is noticed within it), each time saying
// whether the message has gone into Claude Code yet (`TurnBeat`), so a turn the workflow moves
// to another replica resends it, or resumes with the restart message instead. The activity's
// cancellation (the workflow's interrupt) stops the turn. A turn its worker's shutdown cuts
// (SIGTERM: the worker cancels it past its grace, or the service closes its process) fails
// with the retryable ShuttingDown, which moves the session elsewhere; one that finished in
// the grace is returned as it is.

export const HEARTBEAT_MS = 5_000

export type TurnActivityOptions = {
  heartbeatMs?: number
  /** Tests only: how a beat goes out (`context.heartbeat` when omitted), so a test can stop a replica's beats. */
  beat?: (context: Context, beat: TurnBeat) => void
}

export function sessionTurnActivity(host: TurnHost, options: TurnActivityOptions = {}) {
  return {
    [SESSION_TURN_ACTIVITY]: async (input: SessionTurnInput): Promise<SessionTurnResult> => {
      const context = Context.current()
      let beat: TurnBeat = { pushed: false }
      const send = () => (options.beat ? options.beat(context, beat) : context.heartbeat(beat))
      send()
      const timer = setInterval(send, options.heartbeatMs ?? HEARTBEAT_MS)
      try {
        const result = await host.run(
          input,
          (next) => {
            beat = next
            send()
          },
          context.cancellationSignal,
        )
        const cut = result.status !== 'done' && result.status !== 'budget_exhausted'
        if (cut && (host.stopping || context.cancellationDetails?.workerShutdown)) {
          throw ApplicationFailure.retryable('the agent service is shutting down', SHUTTING_DOWN_FAILURE, beat)
        }
        return result
      } finally {
        clearInterval(timer)
      }
    },
  }
}
