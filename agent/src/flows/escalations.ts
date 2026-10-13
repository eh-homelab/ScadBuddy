import type { Sql } from 'postgres'
import type { Principal } from '../auth/principal.js'
import type { RespondArgs, RespondOutcome } from '../gate/durable.js'
import { parseRequestId } from '../gate/ids.js'
import { isUuid } from '../harness/stateDirs.js'
import { principalHeader } from '../tools/authorship.js'

// A flow run's calls outside its grant, at the starting session (#2482, spec 2026-10-11
// §4.2, §4.4, §4.6). The session that started the run is asked first, for its hop; it may
// deny the call or pass it up to the person, never approve it.
//
// Who may answer, and which call, come from the entry's own record, never from the
// request: a child's entry (`ai_pending_input`) names its parent session and the
// principal that owned it when the run started, as the backend's verdict gave them to
// the gate; a flow-own entry (`flow:`) is the backend's, which checks the same against
// the run's `started_by`. The caller is the session its turn runs in (the tool's
// context, set by the session manager or the session's own workflow), which must be
// that parent session, still owned by that principal (a handoff ends the hop), and
// the caller must be its owner. A child's call is an agent tool's: it is never
// narrowed (`flows/narrowing.py` on the backend), only denied or passed up.

export type EscalationAction = 'deny' | 'narrow' | 'pass_up'
export const ESCALATION_ACTIONS = ['deny', 'narrow', 'pass_up'] as const

/** The child session's `respond` Update (gate/durable.ts DurableGate). */
export type EscalationDurable = {
  respond(sessionId: string, args: RespondArgs): Promise<RespondOutcome>
}

export type EscalationDeps = {
  sql: Sql
  /** The durable gate, when Temporal is configured. */
  durable: () => EscalationDurable | undefined
}

export type EscalationCaller = { principal: Pick<Principal, 'id'>; session: string | undefined }

export type EscalationOutcome = { request_id: string; outcome: 'denied' | 'passed_up' | 'approved' | 'with_person' }

export class EscalationError extends Error {
  override name = 'EscalationError'
  readonly status: 403 | 404 | 409 | 422 | 503
  constructor(status: 403 | 404 | 409 | 422 | 503, message: string) {
    super(message)
    this.status = status
  }
}

const NOT_PARENT = 'only the session that started the run may answer for it here, while it still belongs to whoever started the run'
const WITH_PERSON = 'that call is no longer with the starting session: the person decides it now'

type Entry = {
  session_id: string
  hop: 'parent' | 'person' | null
  due: boolean
  parent_session: string | null
  parent_principal: string | null
}

/** One step of an entry's way up (`hops`, spec §4.6). */
function step(action: string, actor: string, reason?: string): Record<string, string> {
  return { hop: 'parent', action, actor, at: new Date().toISOString(), ...(reason ? { reason } : {}) }
}

export class Escalations {
  readonly #deps: EscalationDeps

  constructor(deps: EscalationDeps) {
    this.#deps = deps
  }

  /**
   * The parent's answer to a child's call `requestId` (`durable:`): `deny` or `pass_up`.
   * A flow-own call (`flow:`) is the backend's to authorise (tools/flows.ts sends it).
   */
  async respond(
    caller: EscalationCaller,
    requestId: string,
    action: EscalationAction,
    options: { reason?: string | undefined },
  ): Promise<EscalationOutcome> {
    const parsed = parseRequestId(requestId)
    if (parsed?.store !== 'durable') throw new EscalationError(404, `no call ${requestId} is waiting for the starting session`)
    if (caller.session === undefined || !isUuid(caller.session)) throw new EscalationError(403, NOT_PARENT)
    const entry = await this.#entry(requestId)
    if (!entry) throw new EscalationError(404, `no call ${requestId} is waiting for the starting session`)
    if (entry.parent_session === null || entry.parent_principal === null) {
      throw new EscalationError(entry.hop === 'person' ? 409 : 403, entry.hop === 'person' ? WITH_PERSON : NOT_PARENT)
    }
    await this.#authorise(caller, entry.parent_session, entry.parent_principal)
    if (entry.hop !== 'parent' || entry.due) throw new EscalationError(409, WITH_PERSON)
    if (action === 'narrow') {
      throw new EscalationError(422, "an agent tool's call is never narrowed: deny it, or pass it up to the person")
    }
    const actor = `session:${caller.session}`
    if (action === 'pass_up') {
      const moved = await this.#deps.sql`
        UPDATE ai_pending_input
        SET hop = 'person', hop_until = NULL, hops = hops || ${this.#deps.sql.json([step('pass_up', actor, options.reason)])}
        WHERE request_id = ${requestId} AND hop = 'parent' AND parent_session = ${caller.session}::uuid AND hop_until > now()
        RETURNING request_id`
      if (moved.length === 0) throw new EscalationError(409, WITH_PERSON)
      return { request_id: requestId, outcome: 'passed_up' }
    }
    const durable = this.#deps.durable()
    if (!durable) throw new EscalationError(503, 'durable sessions need Temporal, and this agent service has none configured')
    // The child's workflow validates it as role `parent`: an approval of a run's call,
    // and a deny only (gate/validate.ts). Its resolution records the hop.
    const out = await durable.respond(entry.session_id, {
      request_id: requestId,
      response: { kind: 'approval', decision: 'deny' },
      responder: { kind: 'session', id: caller.session, label: 'Starting session' },
      role: 'parent',
    })
    return { request_id: requestId, outcome: out.outcome === 'denied' ? 'denied' : 'with_person' }
  }

  /** Entries whose parent hop is over go to the person (spec §4.6): never denied. */
  async moveDue(): Promise<number> {
    const moved = await this.#deps.sql`
      UPDATE ai_pending_input
      SET hop = 'person', hop_until = NULL, hops = hops || ${this.#deps.sql.json([step('timeout', 'system')])}
      WHERE hop = 'parent' AND hop_until <= now()
      RETURNING request_id`
    return moved.length
  }

  /** Runs `moveDue` every `intervalMs` (the agent's 30 s gate sweep, spec §4.6); returns the stop. */
  startSweeper(intervalMs: number, options: { ready?: () => Promise<boolean>; onError?: (err: unknown) => void } = {}): () => void {
    const ready = options.ready ?? (() => Promise.resolve(true))
    let running = false
    const timer = setInterval(() => {
      if (running) return
      running = true
      ready()
        .then((ok) => (ok ? this.moveDue() : 0))
        .catch((err: unknown) => options.onError?.(err))
        .finally(() => {
          running = false
        })
    }, intervalMs)
    timer.unref()
    return () => clearInterval(timer)
  }

  async #entry(requestId: string): Promise<Entry | undefined> {
    const [row] = await this.#deps.sql<Entry[]>`
      SELECT session_id::text, hop, coalesce(hop_until <= now(), false) AS due, parent_session::text, parent_principal
      FROM ai_pending_input WHERE request_id = ${requestId} AND flow_run_id IS NOT NULL`
    return row
  }

  /** The caller's session is the parent, still owned by the run's starter, and the caller is its owner. */
  async #authorise(caller: EscalationCaller, parentSession: string, parentPrincipal: string): Promise<void> {
    if (caller.session !== parentSession) throw new EscalationError(403, NOT_PARENT)
    const [owner] = await this.#deps.sql<{ owner_id: string }[]>`
      SELECT owner_id FROM ai_sessions WHERE id = ${parentSession}::uuid AND archived_at IS NULL`
    if (!owner || principalHeader(owner.owner_id) !== parentPrincipal || owner.owner_id !== caller.principal.id) {
      throw new EscalationError(403, NOT_PARENT)
    }
  }
}
