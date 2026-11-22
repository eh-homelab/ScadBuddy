import { CrudReadError, crudRead, wellFormed } from '../api/crudRead.js'
import { type AuditContext, type AuditSink, safeDetail, SYSTEM_ACTOR } from '../audit/log.js'
import type { EventLog } from './eventLog.js'
import { SessionError, type SessionRecord } from './manager.js'
import { type Owner, type ServerEvent, sameOwner } from './protocol.js'
import { sessionPath, type Who } from './rows.js'
import { ROW_WRITE_TIMEOUT_MS } from './turnRows.js'
import type { WarmSessions } from './warm.js'

// Renaming a session, marking it done (#795) and archiving it (#1885); PATCH
// /api/v1/ai/sessions/:id, routes/sessions.ts. Design:
// docs/superpowers/specs/2026-10-09-session-switcher-design.md §6 and "Archive".
//
// Owner-only, like a send or a budget raise: another principal's session is taken
// over first. Done is the terminal status a send already refuses (`closed`); a fork
// still continues it. A rename shows through the panel's snapshot, which the chat
// socket re-reads; done is also a `session.status` event, so open panels show it at once.
//
// Archive (`archived_at`) puts a chat away without marking it done (its workflow ends, and
// the first send after an unarchive starts a new one, #7031): the snapshot and the
// default lists leave it out, a send or a handoff is refused (`archived`) and never
// unarchives it, and a fork is allowed. It is refused, like done, while a turn runs,
// and also while anything is parked on the user (an approval, a question, an
// attention request, a durable session's entry), so an archived chat never holds
// something the badge counts. An undismissed `done` summary does not block it: it
// is dismissed in the same transaction, so it leaves the badge with the chat.
//
// The edit's transaction is `EditRows.edit` (#6449): `CrudEditRows`, the API's route (backend
// api/crud_agent_edits.py, area `session-edits`), which runs it as one transaction there (#5299
// D2). The edit is sent untold first: the route checks every refusal before `untold`, its last, so `untold`
// means the edit would pass and nothing was written. Only then is the workflow told and the edit
// sent again, told. A refusal, a 503 or a lost answer on the first call ends no workflow. What
// changes between the two calls (a turn claimed, a call parked) can still refuse the second with
// the workflow told, and a 503 or a lost answer on the told send, tried twice, leaves the
// workflow told and the row not done: either way the edit sent again marks it done (a send
// meanwhile waits for that workflow to close, within its deadline, then starts a new run, durable.ts). An archive
// needs no telling first: its workflow is told to end once the archive is written, best-effort
// (#7031), since a session archived with its workflow still running loses nothing. When the told send's first answer
// is lost after it committed, the retry finds the row already archived and answers no dismissed
// `done` summaries, so those dismissals (their events committed on the API) get no audit row here.

export type SessionEdit = { title?: string; done?: true; archived?: boolean }

export type SessionEditsDeps = {
  /** The edit's transaction (#6449). */
  rows: EditRows
  events: Pick<EventLog, 'committed'>
  get: (id: string, principal: Owner) => Promise<SessionRecord>
  audit?: AuditSink
  /** Ends a durable session's workflow (#1056); refuses when it cannot be told. */
  endDurable: (id: string) => Promise<void>
  /** Tells an archived session's workflow to end (#7031), best-effort; absent with no Temporal. */
  endArchived?: ((id: string) => Promise<void>) | undefined
  /** Ends a session's background tasks (src/background/) when it is archived or marked done. */
  endBackground?: ((id: string, why: string) => Promise<unknown>) | undefined
  /** This replica's warm processes (warm.ts): a session marked done or archived loses its own. */
  warm?: Pick<WarmSessions, 'evict'>
}

/** Why a `done` summary dismissed by an archive ended, on its card and its audit row. */
export const ARCHIVED_REASON = 'the chat was archived'

/** A `done` summary an archive dismissed, which the edit audits. */
export type Dismissed = { id: string; turn_id: string; tool: string; tool_use_id: string; created_at: Date }

/** What an edit asks: who asks, and the changes. */
export type EditWrite = { principal: Who; title?: string | undefined; done: boolean; archived?: boolean | undefined }

/** What an edit wrote: its events and their seqs, and the summaries it dismissed. */
export type Edited = { logged?: { events: ServerEvent[]; seqs: number[] } | undefined; dismissed: Dismissed[] }

export interface EditRows {
  /**
   * The edit in one transaction, or a SessionError (not_found, busy) and nothing written.
   * `endDurable` tells the workflow it is done, only once the edit's other checks passed.
   */
  edit(id: string, write: EditWrite, endDurable: (id: string) => Promise<void>): Promise<Edited>
}

const busyDone = (id: string) => new SessionError('busy', `session ${id} is running a turn; Stop it first, then mark it done`)
const busyArchive = (id: string) => new SessionError('busy', `session ${id} is running a turn; Stop it first, then archive it`)
const parkedArchive = (id: string) =>
  new SessionError('busy', `session ${id} has an approval, a question or a request waiting for you; answer it, or Stop the turn, then archive it`)
const changedOwner = (id: string) => new SessionError('busy', `session ${id} changed owner meanwhile; try again`)

type WireDismissed = Omit<Dismissed, 'created_at'> & { created_at: string }

/** The API's route (area `session-edits`). */
export class CrudEditRows implements EditRows {
  readonly #base: string
  readonly #fetch: typeof fetch

  /** Each call's request: past the route's own waits (turnRows.ts `ROW_WRITE_TIMEOUT_MS`). */
  readonly timeoutMs = ROW_WRITE_TIMEOUT_MS

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  async edit(id: string, write: EditWrite, endDurable: (id: string) => Promise<void>): Promise<Edited> {
    const path = sessionPath(id)
    // Not a UUID: no session.
    if (path === undefined) throw new SessionError('not_found', `no session ${id}`)
    const send = (told: boolean) =>
      crudRead<{ logged: { session_id: string; events: ServerEvent[]; seqs: number[] }[]; dismissed: WireDismissed[] }>(
        this.#fetch,
        this.#base,
        `an edit of session ${id}`,
        `${path}/edit`,
        {
          method: 'POST',
          timeoutMs: this.timeoutMs,
          // Well-formed: the API refuses a lone surrogate in a title (crudRead.ts).
          body: wellFormed({
            principal: { kind: write.principal.kind, id: write.principal.id },
            title: write.title ?? null,
            done: write.done,
            archived: write.archived ?? null,
            told,
          }),
        },
      )
    let answer
    try {
      // First untold. The route checks `untold` last, so that answer means every other check
      // passed and nothing was written; only then is the workflow told, and the edit sent again.
      // A refusal, a 503 or a lost answer on the first call ends no workflow.
      answer = await send(false).catch(async (error: unknown) => {
        if (!(error instanceof CrudReadError && error.fromRoute && error.status === 409 && error.reason === 'untold')) throw error
        await endDurable(id)
        // Told now: one more try of a told send whose answer was lost or that met a busy row or
        // pool (503), so the window below is one failure wider at most.
        return send(true).catch((again: unknown) => {
          const answered = again instanceof CrudReadError && again.fromRoute
          if (answered && again.status !== 503) throw again
          return send(true)
        })
      })
    } catch (error) {
      if (error instanceof CrudReadError && error.fromRoute) {
        if (error.status === 404 && error.reason === 'gone') throw new SessionError('not_found', `no session ${id}`)
        if (error.status === 409) {
          if (error.reason === 'busy_done') throw busyDone(id)
          if (error.reason === 'busy_archive') throw busyArchive(id)
          if (error.reason === 'parked') throw parkedArchive(id)
          if (error.reason === 'changed_owner') throw changedOwner(id)
        }
      }
      throw error
    }
    const events = answer.logged.flatMap((b) => b.events)
    const seqs = answer.logged.flatMap((b) => b.seqs)
    return {
      logged: events.length ? { events, seqs } : undefined,
      dismissed: answer.dismissed.map((r) => ({ ...r, created_at: new Date(r.created_at) })),
    }
  }
}

export class SessionEdits {
  private readonly deps: SessionEditsDeps

  constructor(deps: SessionEditsDeps) {
    this.deps = deps
  }

  /**
   * With `context`, a successful edit is a `resource` row on the session (action
   * `session_update`, #2159), as a fork is; a refused or failed one is recorded by
   * app.ts's failures-only mount from the route's status.
   */
  async update(id: string, principal: Owner, edit: SessionEdit, context?: Omit<AuditContext, 'actor'>): Promise<SessionRecord> {
    const startedAt = new Date()
    const session = await this.deps.get(id, principal)
    if (!sameOwner(principal, session.owner)) {
      throw new SessionError('forbidden', `session ${id} is controlled by ${session.owner.label}; take it over first`)
    }
    const title = edit.title?.trim()
    if (title === '') throw new SessionError('invalid', 'a title cannot be empty')
    const done = edit.done === true
    const archive = edit.archived === true
    const unarchive = edit.archived === false
    const { logged, dismissed } = await this.deps.rows.edit(
      id,
      { principal, title, done, archived: archive ? true : unarchive ? false : undefined },
      (sid) => this.deps.endDurable(sid),
    )
    if (logged) this.deps.events.committed(id, logged.events, logged.seqs)
    // An archived chat runs nothing (#7031): its workflow ends and releases its warm place, as a
    // done one's does. Best-effort, once the edit passed: a workflow that misses it keeps waiting,
    // and the archive stands. A send after an unarchive starts a new run (durable.ts).
    if (archive && this.deps.endArchived) {
      await this.deps.endArchived(id).catch(() => {
        console.warn(`session edit: archived session ${id}'s workflow could not be told to end`)
      })
    }
    // Its cron jobs, wakeups and monitors end with it: nothing fires into an archived or done chat.
    if (archive || done) await this.deps.endBackground?.(id, archive ? 'the chat was archived' : 'the chat was marked done')
    if (done || archive) void this.deps.warm?.evict(id, done ? 'marked done' : 'archived')
    if (context) {
      const changes = [
        ...(title === undefined ? [] : ['renamed']),
        ...(done ? ['marked done'] : []),
        ...(archive ? ['archived'] : unarchive ? ['unarchived'] : []),
      ]
      await this.deps.audit?.record({
        kind: 'resource',
        action: 'session_update',
        surface: context.surface,
        actor: principal,
        clientIp: context.clientIp,
        sessionId: id,
        outcome: 'ok',
        detail: changes.join(', ') || 'no change',
        startedAt,
        finishedAt: new Date(),
      })
    }
    // Audited as a cancelled question, as a classic one cancelled when its turn ended was.
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

}
