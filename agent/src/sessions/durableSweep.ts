import { type Client, WorkflowNotFoundError } from '@temporalio/client'
import type { Sql } from 'postgres'
import { sessionWorkflowId } from '../gate/durable.js'
import type { EventLog } from './eventLog.js'
import { event, type ServerEvent } from './protocol.js'

// The sweep of durable sessions left `running` (#2001, plan 5c Ruling 16). A durable
// turn takes no lease, so SessionManager.reapExpired never sees one; its end is
// finish_turn's, run by `session-<id>`. A claim is left with no turn behind it when the
// workflow ends (terminated, failed, reset) before finish_turn runs, or when a kept
// claim's update-with-start never reached Temporal, so the workflow was never started.
// Every later send is then refused `busy`. Each pass, a durable row `running` with no
// write for `graceMs` is described; when its workflow is closed or not found, the
// turn's end is written as finish_turn writes a failed turn (a `turn_failed` error,
// then the status), but to `idle`: the next send either starts the workflow afresh or is
// refused `closed`. The UPDATE is guarded on the claim read (still `running`, same
// event_seq), so a turn that claimed the row meanwhile is never ended here.

export type SessionRunState = 'open' | 'closed' | 'not_found'
/** Whether a session's workflow (its latest run) is running, has ended, or never started. */
export type DescribeSession = (workflowId: string) => Promise<SessionRunState>

/**
 * Past DurableTurns' 10 s send timeout many times over, so an update-with-start still
 * in flight is never taken for one that never arrived (plan 5c Ruling 17).
 */
export const RUNNING_GRACE_MS = 120_000
/** How long one describe may take before the row is left for the next pass. */
const DESCRIBE_DEADLINE_MS = 3_000
const BATCH = 100

const LOST: Record<Exclude<SessionRunState, 'open'>, string> = {
  closed: "the turn ended without a result: the session's workflow has ended",
  not_found: "the turn never started: the session's workflow could not be found; send the message again",
}

export type DurableRunningSweepDeps = {
  sql: Sql
  events: EventLog
  describe: DescribeSession
  /** A row written to within this long is not described (default RUNNING_GRACE_MS). */
  graceMs?: number
}

export class DurableRunningSweep {
  readonly #deps: DurableRunningSweepDeps
  readonly #graceMs: number

  constructor(deps: DurableRunningSweepDeps) {
    this.#deps = deps
    this.#graceMs = deps.graceMs ?? RUNNING_GRACE_MS
  }

  /** One pass; returns the sessions reset. */
  async sweep(): Promise<string[]> {
    const { sql, events } = this.#deps
    const rows = await sql<{ id: string; event_seq: string }[]>`
      SELECT id, event_seq FROM ai_sessions
      WHERE mode = 'durable' AND status = 'running'
        AND updated_at < now() - make_interval(secs => ${this.#graceMs / 1000})
      ORDER BY updated_at LIMIT ${BATCH}`
    const reset: string[] = []
    for (const row of rows) {
      let state: SessionRunState
      try {
        state = await this.#deps.describe(sessionWorkflowId(row.id))
      } catch {
        continue
      }
      if (state === 'open') continue
      const tail: ServerEvent[] = [
        event({ type: 'error', sessionId: row.id, code: 'turn_failed', message: LOST[state] }),
        event({ type: 'session.status', sessionId: row.id, status: 'idle' }),
      ]
      const done = await sql.begin(async (tx) => {
        const [claimed] = await tx`
          UPDATE ai_sessions SET status = 'idle', updated_at = now()
          WHERE id = ${row.id} AND mode = 'durable' AND status = 'running' AND event_seq = ${row.event_seq}
          RETURNING id`
        return claimed ? await events.append(row.id, tail, tx) : undefined
      })
      if (!done) continue
      events.committed(row.id, tail, done)
      reset.push(row.id)
    }
    return reset
  }

  /** Runs `sweep` every `intervalMs`, as PendingInputSweep does; returns the stop. */
  start(intervalMs: number, options: { ready?: () => Promise<boolean>; onError?: (err: unknown) => void } = {}): () => void {
    const ready = options.ready ?? (() => Promise.resolve(true))
    let running = false
    const timer = setInterval(() => {
      if (running) return
      running = true
      ready()
        .then((ok) => (ok ? this.sweep() : []))
        .catch((err: unknown) => options.onError?.(err))
        .finally(() => {
          running = false
        })
    }, intervalMs)
    timer.unref()
    return () => clearInterval(timer)
  }
}

/** DescribeSession over a Temporal client: the latest run's status, within a deadline. */
export function durableDescriber(client: Client): DescribeSession {
  return async (workflowId) => {
    try {
      const described = await client.withDeadline(Date.now() + DESCRIBE_DEADLINE_MS, () =>
        client.workflow.getHandle(workflowId).describe(),
      )
      return described.status.name === 'RUNNING' ? 'open' : 'closed'
    } catch (err) {
      if (err instanceof WorkflowNotFoundError) return 'not_found'
      throw err
    }
  }
}
