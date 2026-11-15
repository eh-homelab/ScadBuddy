import type { Sql, TransactionSql } from 'postgres'
import type { EventLog } from '../../sessions/eventLog.js'
import { POOL_BUDGET, POOL_COST } from '../../sessions/manager.js'
import { event, type ServerEvent, type SessionStatus } from '../../sessions/protocol.js'
import { wordTail } from '../../sessions/turnCore.js'
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
// closing events when its activity built them (`tail`, worded here), else built here, as the
// `session.result` with the lineage's spend and budget, the error when the turn did not
// end well, and the final `session.status`, in the transaction that sets the status. It
// writes only while the session is still running this turn, so a retried activity writes
// nothing twice, and never over the next turn: once the row is idle a send may claim it
// (DurableTurns.send) before the workflow has heard this activity's answer.
//
// The queue (#6386, workflow.ts): given the queue's head (`next`), the end claims the row for it in
// the same transaction, so no send takes the idle row first; `session.begin_turn` makes the same
// claim for a head queued while no turn ran; `session.queue_changed` writes the queue as a
// `session.queue` event.

const RUNNING = ['running', 'waiting_approval', 'waiting_input']
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
export const QUEUED_DROPPED = 'queued_message_dropped'

type ClaimRow = {
  status: SessionStatus
  durable_turn_id: string | null
  owner_kind: string
  owner_id: string
  archived: boolean
  runtime: string | null
  spent: string | null
  budget: string
}

/** Why the session cannot run a queued message now; undefined when it can (DurableTurns.send's claim condition). */
function whyNot(row: ClaimRow, next: QueuedMessage): string | undefined {
  const author = next.author
  if (row.status === 'done') return 'the session is done'
  if (row.archived) return 'the session is archived'
  if (row.runtime === 'durable') return "the session's conversation ended with the old session runtime"
  if (!(row.owner_kind === author.kind && (row.owner_id === author.id || row.owner_kind === 'browser'))) return 'the session was handed off'
  if (Number(row.spent ?? 0) >= Number(row.budget)) return 'the session has spent its budget'
  return undefined
}

/**
 * Claims the row for the queue's head in `tx` (#6386): the claim a send makes (durable.ts
 * `DurableTurns.send`), and its `user.turn` and `session.status running`. `busy` while the row
 * runs another turn; `dropped`, with an `error` event that says why, when the session cannot run
 * it. A head already claimed (a retry) is `started` and writes nothing.
 */
async function claimQueued(tx: TransactionSql, sessionId: string, next: QueuedMessage, events: EventLog): Promise<{ outcome: BeginOutcome; written: ServerEvent[]; seqs: number[] }> {
  const none = { written: [] as ServerEvent[], seqs: [] as number[] }
  const [row] = await tx.unsafe<ClaimRow[]>(
    `SELECT status, durable_turn_id, owner_kind, owner_id, archived_at IS NOT NULL AS archived, runtime,
       ${POOL_COST} AS spent, ${POOL_BUDGET} AS budget
     FROM ai_sessions WHERE id = $1 FOR UPDATE`,
    [sessionId],
  )
  // The session is gone (forgotten): nothing to write.
  if (!row) return { outcome: 'dropped', ...none }
  if (row.durable_turn_id === next.turn_id) return { outcome: 'started', ...none }
  if (RUNNING.includes(row.status)) return { outcome: 'busy', ...none }
  const why = whyNot(row, next)
  if (why !== undefined) {
    const written = [event({ type: 'error', sessionId, code: QUEUED_DROPPED, message: `A queued message was not sent: ${why}.` })]
    return { outcome: 'dropped', written, seqs: await events.append(sessionId, written, tx) }
  }
  const author = next.author
  await tx`
    UPDATE ai_sessions SET status = 'running',
      turn_tiers = ${next.tiers ? tx.json({ by: { kind: author.kind, id: author.id }, tiers: [...next.tiers] }) : null},
      durable_turn_id = ${next.turn_id}, turn_flow = ${next.bare === true}, updated_at = now(), turn_started_at = now(),
      interrupt_requested = false
    WHERE id = ${sessionId}`
  const written = [
    event({
      type: 'user.turn',
      sessionId,
      turnId: next.turn_id,
      text: next.words,
      author,
      ...(next.sent?.length ? { images: next.sent } : {}),
    }),
    event({ type: 'session.status', sessionId, status: 'running' }),
  ]
  return { outcome: 'started', written, seqs: await events.append(sessionId, written, tx) }
}

/** What a turn's end reports after it is written (sessions/orderCosts.ts `OrderCosts`); never throws. */
export type TurnEndReports = { report(sessionId: string): Promise<void> }

export function sessionFinishActivities(sql: Sql, events: EventLog, costs?: TurnEndReports) {
  return {
    [SESSION_FINISH_ACTIVITY]: async (args: FinishTurnInput): Promise<boolean | FinishTurnResult> => {
      const sessionId = args.sessionId
      const { next } = args
      let started: Awaited<ReturnType<typeof claimQueued>> | undefined
      const written = await sql.begin(async (tx) => {
        const ended = await endTurn(tx)
        // The queue's head, in the same transaction: the row never goes idle for a send to take.
        if (next) started = await claimQueued(tx, sessionId, next, events)
        return ended
      })
      async function endTurn(tx: TransactionSql) {
        const [row] = await tx.unsafe<{ turns: number; budget: string; spent: string | null }[]>(
          `SELECT ai_sessions.turns, ${POOL_BUDGET} AS budget, ${POOL_COST} AS spent
           FROM ai_sessions WHERE id = $1 AND status = ANY($2) AND durable_turn_id = $3::uuid FOR UPDATE`,
          [sessionId, RUNNING, args.turnId],
        )
        // Written already, the next turn has the row, or the session is gone.
        if (!row) return undefined
        const spent = Number(row.spent ?? 0)
        const budget = Number(row.budget)
        // The turn's own closing events, as a classic turn builds them (turnCore.ts `tallyTurn`), worded
        // with the row's spend and budget; built here only for an end the workflow decided.
        const tail = args.tail?.length
          ? wordTail(args.tail, { spentUsd: spent, budgetUsd: budget, turns: Number(row.turns) })
          : turnEvents(sessionId, args, spent, budget, Number(row.turns))
        const last = tail.at(-1) as Extract<ServerEvent, { type: 'session.status' }>
        await tx`UPDATE ai_sessions SET status = ${last.status}, durable_turn_id = NULL, interrupt_requested = false, updated_at = now() WHERE id = ${sessionId}`
        return { tail, seqs: await events.append(sessionId, tail, tx) }
      }
      if (written) events.committed(sessionId, written.tail, written.seqs)
      if (started?.written.length) events.committed(sessionId, started.written, started.seqs)
      // The session's spend on the orders it revised (#5330), with this turn's in it. Also on a
      // retry that wrote nothing. Not awaited: a slow backend never holds up the next turn, and
      // a report cut off with its process is still pending at the next turn's end.
      void costs?.report(sessionId)
      return started ? { written: written !== undefined, next: started.outcome } : written !== undefined
    },

    /** Starts a message queued while no turn ran (#6386): the same claim as the end's `next`. */
    [SESSION_BEGIN_TURN_ACTIVITY]: async ({ sessionId, next }: BeginTurnInput): Promise<BeginOutcome> => {
      const claimed = await sql.begin((tx) => claimQueued(tx, sessionId, next, events))
      if (claimed.written.length) events.committed(sessionId, claimed.written, claimed.seqs)
      return claimed.outcome
    },

    /** Writes the queue after a change as a `session.queue` event (#6386); nothing for a session that is gone. */
    [SESSION_QUEUE_CHANGED_ACTIVITY]: async ({ sessionId, version, queue }: QueueChangedInput): Promise<boolean> => {
      const written = await sql.begin(async (tx) => {
        const [row] = await tx`SELECT 1 FROM ai_sessions WHERE id = ${sessionId} FOR UPDATE`
        if (!row) return undefined
        const tail = [event({ type: 'session.queue', sessionId, version, queue })]
        return { tail, seqs: await events.append(sessionId, tail, tx) }
      })
      if (written) events.committed(sessionId, written.tail, written.seqs)
      return written !== undefined
    },
  }
}
