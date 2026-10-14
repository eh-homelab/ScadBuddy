import type { Sql } from 'postgres'
import type { EventLog } from '../../sessions/eventLog.js'
import { POOL_BUDGET, POOL_COST } from '../../sessions/manager.js'
import { event, type ServerEvent } from '../../sessions/protocol.js'
import { MESSAGE_MAX, type FinishTurnInput } from './models.js'
import { SESSION_FINISH_ACTIVITY } from './names.js'

// A turn's end (`session.finish_turn`), the port of agent-durable's `finish_turn`
// (session/activities.py): what a classic turn's end writes (manager.ts), as the
// `session.result` with the lineage's spend and budget, the error when the turn did not
// end well, and the final `session.status`, in the transaction that sets the status. It
// writes only while the session is still running a turn, so a retried activity writes
// nothing twice.

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

export function sessionFinishActivities(sql: Sql, events: EventLog) {
  return {
    [SESSION_FINISH_ACTIVITY]: async (args: FinishTurnInput): Promise<boolean> => {
      const sessionId = args.sessionId
      const written = await sql.begin(async (tx) => {
        const [row] = await tx.unsafe<{ turns: number; budget: string; spent: string | null }[]>(
          `SELECT ai_sessions.turns, ${POOL_BUDGET} AS budget, ${POOL_COST} AS spent
           FROM ai_sessions WHERE id = $1 AND status = ANY($2) FOR UPDATE`,
          [sessionId, RUNNING],
        )
        // Written already, or the session is gone.
        if (!row) return undefined
        const tail = turnEvents(sessionId, args, Number(row.spent ?? 0), Number(row.budget), Number(row.turns))
        const last = tail.at(-1) as Extract<ServerEvent, { type: 'session.status' }>
        await tx`UPDATE ai_sessions SET status = ${last.status}, durable_turn_id = NULL, updated_at = now() WHERE id = ${sessionId}`
        return { tail, seqs: await events.append(sessionId, tail, tx) }
      })
      if (!written) return false
      events.committed(sessionId, written.tail, written.seqs)
      return true
    },
  }
}
