import type { Sql } from 'postgres'
import { type AuditLog, safeDetail, SYSTEM_ACTOR } from '../audit/log.js'
import { inputResolved } from '../gate/classic.js'
import { isDoneSummary } from '../questions/waiting.js'
import type { EventLog } from './eventLog.js'
import { SessionError, type SessionRecord } from './manager.js'
import { event, type Owner, type ServerEvent, sameOwner } from './protocol.js'

// Renaming a session, marking it done (#795) and archiving it (#1885); PATCH
// /api/v1/ai/sessions/:id, routes/sessions.ts. Design:
// docs/superpowers/specs/2026-10-09-session-switcher-design.md §6 and "Archive".
//
// Owner-only, like a send or a budget raise: another principal's session is taken
// over first. Done is the terminal status a send already refuses (`closed`); a fork
// still continues it. A rename shows through the panel's snapshot, which the chat
// socket re-reads; done is also a `session.status` event, so open panels show it at once.
//
// Archive (`archived_at`) puts a chat away without ending it: the snapshot and the
// default lists leave it out, a send or a handoff is refused (`archived`) and never
// unarchives it, and a fork is allowed. It is refused, like done, while a turn runs,
// and also while anything is parked on the user (an approval, a question, an
// attention request, a durable session's entry), so an archived chat never holds
// something the badge counts. An undismissed `done` summary does not block it: it
// is dismissed in the same transaction, so it leaves the badge with the chat.

export type SessionEdit = { title?: string; done?: true; archived?: boolean }

export type SessionEditsDeps = {
  sql: Sql
  events: Pick<EventLog, 'append' | 'committed'>
  get: (id: string, principal: Owner) => Promise<SessionRecord>
  audit?: AuditLog
  /** Ends a durable session's workflow (#1056); refuses when it cannot be told. */
  endDurable: (id: string) => Promise<void>
}

/** Why a `done` summary dismissed by an archive ended, on its card and its audit row. */
export const ARCHIVED_REASON = 'the chat was archived'

type Before = {
  was: string
  mode: string
  busy: boolean
  parked: boolean
  archived: boolean
}

type Dismissed = { id: string; turn_id: string; tool: string; tool_use_id: string; created_at: Date }

export class SessionEdits {
  private readonly deps: SessionEditsDeps

  constructor(deps: SessionEditsDeps) {
    this.deps = deps
  }

  async update(id: string, principal: Owner, edit: SessionEdit): Promise<SessionRecord> {
    const session = await this.deps.get(id, principal)
    if (!sameOwner(principal, session.owner)) {
      throw new SessionError('forbidden', `session ${id} is controlled by ${session.owner.label}; take it over first`)
    }
    const title = edit.title?.trim()
    if (title === '') throw new SessionError('invalid', 'a title cannot be empty')
    const done = edit.done === true
    const archive = edit.archived === true
    const unarchive = edit.archived === false
    let logged: { events: ServerEvent[]; seqs: number[] } | undefined
    const dismissed = await this.deps.sql.begin(async (tx) => {
      const [before] = await tx<Before[]>`
        SELECT status AS was, mode, archived_at IS NOT NULL AS archived,
               -- A turn holds it: a classic claim, or a durable session's running or waiting status.
               (turn_id IS NOT NULL OR status IN ('running', 'waiting_approval', 'waiting_input')) AS busy,
               (EXISTS (SELECT 1 FROM ai_approvals WHERE session_id = ${id} AND decision IS NULL)
                OR EXISTS (SELECT 1 FROM ai_questions q WHERE q.session_id = ${id} AND q.outcome IS NULL
                           AND NOT ${isDoneSummary(tx, 'q')})
                OR EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = ${id})) AS parked
          FROM ai_sessions
         WHERE id = ${id} AND owner_kind = ${principal.kind} AND owner_id = ${principal.id}
         FOR UPDATE`
      if (!before) throw await this.changedOwner(id)
      if (done && before.busy) {
        throw new SessionError('busy', `session ${id} is running a turn; Stop it first, then mark it done`)
      }
      if (archive && !before.archived) {
        if (before.busy) throw new SessionError('busy', `session ${id} is running a turn; Stop it first, then archive it`)
        if (before.parked) {
          throw new SessionError(
            'busy',
            `session ${id} has an approval, a question or a request waiting for you; answer it, or Stop the turn, then archive it`,
          )
        }
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
      if (done && before.mode !== 'classic' && before.was !== 'done') await this.deps.endDurable(id)
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
      if (events.length) logged = { events, seqs: await this.deps.events.append(id, events, tx) }
      return gone
    })
    if (logged) this.deps.events.committed(id, logged.events, logged.seqs)
    // Audited as a cancelled question, as one cancelled when its turn ends is (questions/service.ts).
    const audit = this.deps.audit
    if (audit && dismissed.length) {
      await Promise.allSettled(
        dismissed.map((r) =>
          audit.record({
            kind: 'question',
            action: 'cancelled',
            surface: 'system',
            actor: SYSTEM_ACTOR,
            sessionId: id,
            turnId: r.turn_id,
            toolUseId: r.tool_use_id,
            tier: 'read',
            outcome: 'refused',
            detail: safeDetail(`${r.tool} question ${r.id}: ${ARCHIVED_REASON}`),
            startedAt: r.created_at,
            finishedAt: new Date(),
          }),
        ),
      )
    }
    return this.deps.get(id, principal)
  }

  /** The row was not the principal's when locked: it is gone, or it changed owner meanwhile. */
  private async changedOwner(id: string): Promise<SessionError> {
    const [now] = await this.deps.sql<{ id: string }[]>`SELECT id FROM ai_sessions WHERE id = ${id}`
    if (!now) return new SessionError('not_found', `no session ${id}`)
    return new SessionError('busy', `session ${id} changed owner meanwhile; try again`)
  }
}
