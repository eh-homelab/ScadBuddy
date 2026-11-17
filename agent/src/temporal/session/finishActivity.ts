import type { Sql } from 'postgres'
import type { SessionImages } from '../../sessions/blobs.js'
import type { EventLog } from '../../sessions/eventLog.js'
import { event, type ServerEvent } from '../../sessions/protocol.js'
import { type Logged, type NextClaim, PgTurnRows, type TurnRows } from '../../sessions/turnRows.js'
import {
  type BeginOutcome,
  type BeginTurnInput,
  MESSAGE_MAX,
  type FinishTurnInput,
  type FinishTurnResult,
  type QueueChangedInput,
  type QueuedMessage,
} from './models.js'
import { SESSION_BEGIN_TURN_ACTIVITY, SESSION_FINISH_ACTIVITY, SESSION_QUEUE_CHANGED_ACTIVITY } from './names.js'

// A turn's end (`session.finish_turn`), the port of agent-durable's `finish_turn`
// (session/activities.py): what a classic turn's end writes (manager.ts): the turn's own
// closing events when its activity built them (`tail`), else built here, worded by the end (turnRows.ts) as the
// `session.result` with the lineage's spend and budget, the error when the turn did not
// end well, and the final `session.status`, in the transaction that sets the status. It
// writes only while the session is still running this turn, so a retried activity writes
// nothing twice, and never over the next turn: once the row is idle a send may claim it
// (DurableTurns.send) before the workflow has heard this activity's answer.
//
// The queue (#6386, workflow.ts): given the queue's head (`next`), the end claims the row for it in
// the same transaction, so no send takes the idle row first; `session.begin_turn` makes the same
// claim for a head queued while no turn ran; `session.queue_changed` writes the queue as a
// `session.queue` event, then deletes the images of the messages that left it unrun (#6413).

const usd = (amount: number): string => `$${amount.toFixed(2)}`

export function turnEvents(sessionId: string, args: FinishTurnInput, spent: number, budget: number, turns: number): ServerEvent[] {
  const events: ServerEvent[] = [event({ type: 'session.result', sessionId, costUsd: spent, turns, budgetUsd: budget })]
  if (args.outcome === 'budget_exhausted') {
    events.push(event({ type: 'error', sessionId, code: 'error_max_budget_usd', message: `this chat used its ${usd(budget)} budget (${usd(spent)} spent)` }))
  } else if (args.outcome === 'interrupted') {
    events.push(event({ type: 'error', sessionId, code: 'interrupted', message: 'the turn was interrupted' }))
  } else if (args.outcome === 'failed') {
    events.push(event({ type: 'error', sessionId, code: args.code ?? 'turn_failed', message: (args.message || 'the turn ended without a result').slice(0, MESSAGE_MAX) }))
  } else if (args.outcome === 'done' && args.code !== undefined) {
    // A turn that ended within its limits but said why it stopped (`error_max_turns`, the time cap): idle, as classic leaves it.
    events.push(event({ type: 'error', sessionId, code: args.code, message: (args.message || `the turn stopped (${args.code})`).slice(0, MESSAGE_MAX) }))
  }
  events.push(event({ type: 'session.status', sessionId, status: args.outcome === 'failed' ? 'failed' : 'idle' }))
  return events
}

/** The code of the `error` event that says a queued message could not start. */
export { QUEUED_DROPPED } from '../../sessions/turnRows.js'

/** The queue's head as the row's claim takes it, with its `user.turn` and `session.status running`. */
function headClaim(sessionId: string, next: QueuedMessage): NextClaim {
  const author = next.author
  return {
    turnId: next.turn_id,
    author: { kind: author.kind, id: author.id },
    tiers: next.tiers ? [...next.tiers] : null,
    bare: next.bare === true,
    started: [
      event({
        type: 'user.turn',
        sessionId,
        turnId: next.turn_id,
        text: next.words,
        author,
        ...(next.sent?.length ? { images: next.sent } : {}),
      }),
      event({ type: 'session.status', sessionId, status: 'running' }),
    ],
  }
}

/** What a turn's end reports after it is written (sessions/orderCosts.ts `OrderCosts`); never throws. */
export type TurnEndReports = { report(sessionId: string): Promise<void> }

/**
 * The activities over `turns`, the row's writes (sessions/turnRows.ts: `PgTurnRows` on `sql` when
 * omitted; gated to the API's routes in main.ts, #6449).
 */
export function sessionFinishActivities(sql: Sql, events: EventLog, costs?: TurnEndReports, blobs?: Pick<SessionImages, 'delete'>, turns?: TurnRows) {
  const rows = turns ?? new PgTurnRows(sql)
  const told = (sessionId: string, logged: Logged | undefined) => {
    if (logged?.events.length) events.committed(sessionId, logged.events, logged.seqs)
  }
  return {
    [SESSION_FINISH_ACTIVITY]: async (args: FinishTurnInput): Promise<boolean | FinishTurnResult> => {
      const sessionId = args.sessionId
      const { next } = args
      // The turn's own closing events, as a classic turn builds them (turnCore.ts `tallyTurn`), or
      // built here for an end the workflow decided; the end words them with the row's spend and budget.
      const tail = args.tail?.length ? args.tail : turnEvents(sessionId, args, 0, 0, 0)
      const done = await rows.finish(sessionId, args.turnId, tail, next ? headClaim(sessionId, next) : undefined)
      told(sessionId, done.ended)
      told(sessionId, done.next?.logged)
      // The session's spend on the orders it revised (#5330), with this turn's in it. Also on a
      // retry that wrote nothing. Not awaited: a slow backend never holds up the next turn, and
      // a report cut off with its process is still pending at the next turn's end.
      void costs?.report(sessionId)
      const written = done.ended !== undefined
      return next ? { written, next: done.next?.outcome ?? 'dropped' } : written
    },

    /** Starts a message queued while no turn ran (#6386): the same claim as the end's `next`. */
    [SESSION_BEGIN_TURN_ACTIVITY]: async ({ sessionId, next }: BeginTurnInput): Promise<BeginOutcome> => {
      const claimed = await rows.begin(sessionId, headClaim(sessionId, next))
      told(sessionId, claimed.logged)
      return claimed.outcome
    },

    /**
     * Writes the queue after a change as a `session.queue` event (#6386); nothing for a session that
     * is gone. Then deletes the images of the messages that left it unrun (#6413): best effort, as an
     * image left behind costs only its row, which goes with the session.
     */
    [SESSION_QUEUE_CHANGED_ACTIVITY]: async ({ sessionId, version, queue, drop }: QueueChangedInput): Promise<boolean> => {
      const written = await rows.queueChanged(sessionId, event({ type: 'session.queue', sessionId, version, queue }))
      told(sessionId, written)
      if (written && blobs) {
        for (const made of drop ?? []) {
          await blobs.delete(sessionId, made).catch((err: unknown) => {
            console.warn(`session ${sessionId}: a queued message's images were not deleted: ${err instanceof Error ? err.message : String(err)}`)
          })
        }
      }
      return written !== undefined
    },
  }
}
