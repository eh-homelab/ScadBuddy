import { Context } from '@temporalio/activity'
import type { Client } from '@temporalio/client'
import { sessionWorkflowId } from '../../gate/durable.js'
import type { CancelReason, TurnBeat, TurnHost } from '../../sessions/turnHost.js'
import type { OpenInputAnswer, OpenInputArgs, SessionTurnInput, SessionTurnResult } from './models.js'
import { OPEN_INPUT_UPDATE, SESSION_TURN_ACTIVITY } from './names.js'

// `session_turn` (spec 2026-10-13 §5.2), on a replica's session host queue only: TurnHost.run
// under the activity's heartbeat. It heartbeats every HEARTBEAT_MS (the workflow's
// heartbeatTimeout is 20 s, so a replica that dies is noticed within it), each time saying
// whether the message has gone into Claude Code yet (`TurnBeat`, advisory: the next placement
// reads the transcript). The activity's cancellation is told apart: the workflow's interrupt
// stops the turn; the worker's shutdown cuts it as TurnHost.stop does (it then fails the
// retryable ShuttingDown, which places it elsewhere); a cancel because the workflow no longer
// waits on it (its heartbeat or StartToClose timed out) cuts it off, writing nothing more.
//
// A call the turn parks (§5.4) is opened by the Update `open_input` on the session's own
// workflow, sent with `client`, the service's client (its data converter seals the payloads
// as the workflow's are): ordinary client use from an activity. The call's tool_use id names
// the Update, so a re-sent one is the same Update.

export const HEARTBEAT_MS = 5_000

export type TurnActivityOptions = {
  /** How a parked call reaches its workflow. Without one, every park is refused (turnHost.ts NO_PARKS). */
  client?: Client
  heartbeatMs?: number
  /** Tests only: how a beat goes out (`context.heartbeat` when omitted), so a test can stop a replica's beats. */
  beat?: (context: Context, beat: TurnBeat) => void
  /** Tests only: the cancellation the turn hears (`context.cancellationSignal` when omitted), so a test can make a replica deaf to it. */
  cancelled?: (context: Context) => AbortSignal
}

function reasonOf(context: Context): CancelReason {
  const details = context.cancellationDetails
  if (details?.workerShutdown) return 'shutdown'
  if (details?.timedOut || details?.notFound) return 'gone'
  return 'interrupt'
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
        return await host.run(input, {
          heartbeat: (next: TurnBeat) => {
            beat = next
            send()
          },
          cancelled: options.cancelled ? options.cancelled(context) : context.cancellationSignal,
          cancelReason: () => reasonOf(context),
          ...(options.client ? { openInput: openInput(options.client, input.sessionId) } : {}),
        })
      } finally {
        clearInterval(timer)
      }
    },
  }
}

function openInput(client: Client, sessionId: string): (args: OpenInputArgs) => Promise<OpenInputAnswer> {
  const handle = client.workflow.getHandle(sessionWorkflowId(sessionId))
  return (args) => handle.executeUpdate<OpenInputAnswer, [OpenInputArgs]>(OPEN_INPUT_UPDATE, { args: [args], updateId: `${OPEN_INPUT_UPDATE}:${args.call.id}` })
}
