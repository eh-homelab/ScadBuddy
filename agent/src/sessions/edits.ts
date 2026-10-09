import type { Sql } from 'postgres'
import type { EventLog } from './eventLog.js'
import { SessionError, type SessionRecord } from './manager.js'
import { event, type Owner, sameOwner } from './protocol.js'

// Renaming a session and marking it done (#795; PATCH /api/v1/ai/sessions/:id,
// routes/sessions.ts). Design: docs/superpowers/specs/2026-10-09-session-switcher-design.md §6.
//
// Owner-only, like a send or a budget raise: another principal's session is taken
// over first. Done is the terminal status a send already refuses (`closed`); a fork
// still continues it. A rename shows through the panel's snapshot, which the chat
// socket re-reads; done is also a `session.status` event, so open panels show it at once.

export type SessionEdit = { title?: string; done?: true }

export type SessionEditsDeps = {
  sql: Sql
  events: Pick<EventLog, 'append'>
  get: (id: string, principal: Owner) => Promise<SessionRecord>
}

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
    const [hit] = await this.deps.sql<{ was: string; mode: string; turn_id: string | null }[]>`
      WITH before AS (
        SELECT status AS was, mode, turn_id FROM ai_sessions
         WHERE id = ${id} AND owner_kind = ${principal.kind} AND owner_id = ${principal.id}
         FOR UPDATE
      )
      UPDATE ai_sessions s
         SET title = coalesce(${title ?? null}::text, s.title),
             status = CASE WHEN ${done} THEN 'done' ELSE s.status END,
             updated_at = now()
        FROM before
       WHERE s.id = ${id} AND s.owner_kind = ${principal.kind} AND s.owner_id = ${principal.id}
         AND (NOT ${done} OR (before.turn_id IS NULL AND before.mode = 'classic'))
      RETURNING before.was, before.mode, before.turn_id`
    if (!hit) throw await this.whyNot(id, principal)
    if (done && hit.was !== 'done') {
      await this.deps.events.append(id, [event({ type: 'session.status', sessionId: id, status: 'done' })])
    }
    return this.deps.get(id, principal)
  }

  private async whyNot(id: string, principal: Owner): Promise<SessionError> {
    const [now] = await this.deps.sql<{ owner_kind: string; owner_id: string; mode: string; turn_id: string | null }[]>`
      SELECT owner_kind, owner_id, mode, turn_id FROM ai_sessions WHERE id = ${id}`
    if (!now) return new SessionError('not_found', `no session ${id}`)
    if (now.owner_kind !== principal.kind || now.owner_id !== principal.id) {
      return new SessionError('busy', `session ${id} changed owner meanwhile; try again`)
    }
    if (now.mode !== 'classic') {
      return new SessionError('busy', `session ${id} is durable; it ends with its workflow, not here (#1056)`)
    }
    return new SessionError('busy', `session ${id} is running a turn; Stop it first, then mark it done`)
  }
}
