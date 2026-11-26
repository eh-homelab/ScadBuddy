import type { Sql, TransactionSql } from 'postgres'
import { inputResolved } from '../../../src/gate/projection.js'
import { ARCHIVED_REASON, type Dismissed, type Edited, type EditRows, type EditWrite } from '../../../src/sessions/edits.js'
import { SessionError } from '../../../src/sessions/manager.js'
import { event, type ServerEvent } from '../../../src/sessions/protocol.js'
import { WARM_EVICT_CHANNEL } from '../../../src/sessions/warmEvict.js'
import { appendEventsIn } from './eventRows.js'

// `EditRows` straight on the test database, the transaction the API's route runs (backend
// tools/go/core/internal/edits), for tests with no API (#5299).

/** True for a posted `done` summary (#1383): reason `done`, no timer. `alias` is the table's alias in the query. */
function isDoneSummary(sql: Sql | TransactionSql, alias?: string) {
  const column = (name: string) => sql(alias ? `${alias}.${name}` : name)
  return sql`(${column('attention_reason')} IS NOT DISTINCT FROM 'done' AND ${column('expires_at')} IS NULL)`
}

const busyDone = (id: string) => new SessionError('busy', `session ${id} is running a turn; Stop it first, then mark it done`)
const busyArchive = (id: string) => new SessionError('busy', `session ${id} is running a turn; Stop it first, then archive it`)
const parkedArchive = (id: string) =>
  new SessionError('busy', `session ${id} has an approval, a question or a request waiting for you; answer it, or Stop the turn, then archive it`)
const changedOwner = (id: string) => new SessionError('busy', `session ${id} changed owner meanwhile; try again`)

type Before = {
  was: string
  busy: boolean
  parked: boolean
  archived: boolean
}

/** The agent's own SQL (until the API lists area `session-edits`). */
export class PgEditRows implements EditRows {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async edit(id: string, write: EditWrite, endDurable: (id: string) => Promise<void>): Promise<Edited> {
    const { principal, title, done } = write
    const archive = write.archived === true
    const unarchive = write.archived === false
    let logged: Edited['logged']
    const dismissed = await this.#sql.begin(async (tx) => {
      const [before] = await tx<Before[]>`
        SELECT status AS was, archived_at IS NOT NULL AS archived,
               -- A turn holds it: running, or parked on one of its calls.
               status IN ('running', 'waiting_approval', 'waiting_input') AS busy,
               (EXISTS (SELECT 1 FROM ai_approvals WHERE session_id = ${id} AND decision IS NULL)
                OR EXISTS (SELECT 1 FROM ai_questions q WHERE q.session_id = ${id} AND q.outcome IS NULL
                           AND NOT ${isDoneSummary(tx, 'q')})
                OR EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = ${id})) AS parked
          FROM ai_sessions
         -- Any browser-kind principal is the browser user's (#4975 phase 5, D8; protocol.ts sameOwner).
         WHERE id = ${id} AND owner_kind = ${principal.kind} AND (owner_id = ${principal.id} OR owner_kind = 'browser')
         FOR UPDATE`
      if (!before) throw await this.#changedOwner(id)
      if (done && before.busy) throw busyDone(id)
      if (archive && !before.archived) {
        if (before.busy) throw busyArchive(id)
        if (before.parked) throw parkedArchive(id)
      }
      await tx`
        UPDATE ai_sessions
           SET title = coalesce(${title ?? null}::text, title),
               status = CASE WHEN ${done} THEN 'done' ELSE status END,
               archived_at = CASE WHEN ${archive} THEN coalesce(archived_at, now())
                                  WHEN ${unarchive} THEN NULL
                                  ELSE archived_at END,
               updated_at = now()
         WHERE id = ${id}`
      // Told inside the transaction: a workflow that cannot be told leaves the row as it was.
      if (done && before.was !== 'done') await endDurable(id)
      // Its warm process on every replica goes with it (spec 2026-10-13 §5.3, warmEvict.ts): sent on commit.
      if ((done && before.was !== 'done') || (archive && !before.archived)) await tx`SELECT pg_notify(${WARM_EVICT_CHANNEL}, ${id})`
      const events: ServerEvent[] = []
      if (done && before.was !== 'done') events.push(event({ type: 'session.status', sessionId: id, status: 'done' }))
      let gone: Dismissed[] = []
      if (archive) {
        gone = await tx<Dismissed[]>`
          UPDATE ai_questions q SET outcome = 'cancelled', reason = ${ARCHIVED_REASON}, resolved_at = now()
           WHERE q.session_id = ${id} AND q.outcome IS NULL AND ${isDoneSummary(tx, 'q')}
          RETURNING q.id, q.turn_id, q.tool, q.tool_use_id, q.created_at`
        for (const r of gone) {
          events.push(event({ type: 'question.resolved', sessionId: id, id: r.id, answered: false, reason: ARCHIVED_REASON }))
          events.push(inputResolved(id, `question:${r.id}`, 'answer', 'cancelled', ARCHIVED_REASON))
        }
      }
      if (events.length) logged = { events, seqs: await appendEventsIn(tx, id, events) }
      return gone
    })
    return { logged, dismissed }
  }

  /** The row was not the principal's when locked: it is gone, or it changed owner meanwhile. */
  async #changedOwner(id: string): Promise<SessionError> {
    const [now] = await this.#sql<{ id: string }[]>`SELECT id FROM ai_sessions WHERE id = ${id}`
    if (!now) return new SessionError('not_found', `no session ${id}`)
    return changedOwner(id)
  }
}
