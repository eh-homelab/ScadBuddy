import { wellFormed } from '../api/crudRead.js'
import { type CrudTarget, crudOf, qs } from '../api/crudStores.js'
import { REQUEST_ID_MAX } from '../gate/crudStore.js'
import { isUuid } from '../harness/stateDirs.js'

// A flow run's calls at the starting session's hop through the API's CRUD routes (#5299 step 10;
// backend api/crud_agent_escalations.py, agent_data/escalations.py), area `escalations`:
// Escalations' SQL on `ai_pending_input` and `ai_escalation_deliveries` (flows/escalations.ts),
// and FlowQueue's read of a run's child entries (flows/queue.ts). Each caller uses its own SQL
// until the API lists the area (D9). Who may answer stays Escalations' check; every write names
// the parent session again, so it moves nothing of another session's.
//
// Ids travel in the body or the query string, never the path. One no row can hold is not sent.

/** A run's child entry, as Escalations checks an answer against it. */
export type EscalationEntry = {
  session_id: string
  hop: 'parent' | 'person' | null
  due: boolean
  claimed: boolean
  parent_session: string | null
  parent_principal: string | null
}

/** A child's call a delivery pass sends. */
export type WaitingRow = {
  request_id: string
  flow_run_id: string
  tool: string
  summary: string
  outside_reason: string | null
  hop_until: Date
  past_grace: boolean
  session_id: string
  parent_session: string
  parent_principal: string
}

/** A run's child entry for its queue: `hop_until` is an ISO time, or `infinity` while a deny's claim holds it. */
export type RunEntryRow = {
  request_id: string
  kind: 'approval' | 'answer'
  session_id: string
  tool: string
  summary: string
  input_hash: string | null
  hop: 'parent' | 'person' | null
  hop_until: string | null
  outside_reason: string | null
  hops: unknown[] | null
  created_at: Date
  expires_at: Date
  expiring: boolean
}

/** The calls of one starter session: a child's (`ai_pending_input`) and a run's own (`flow:`). */
export type SessionCalls = { session: string; child: string[]; own: string[] }

/** Escalations' and FlowQueue's routes. */
export interface EscalationRoutes {
  entry(requestId: string): Promise<EscalationEntry | undefined>
  passUp(requestId: string, session: string, reason: string | undefined): Promise<boolean>
  /** The deadline before the claim, which `releaseDeny` puts back; undefined when nothing was claimed. */
  claimDeny(requestId: string, session: string): Promise<Date | undefined>
  releaseDeny(requestId: string, hopUntil: Date): Promise<void>
  moveDue(): Promise<number>
  waiting(graceS: number): Promise<WaitingRow[]>
  claim(calls: SessionCalls): Promise<string[]>
  unclaim(calls: SessionCalls): Promise<void>
  skip(calls: SessionCalls): Promise<void>
  deliveries(after: string, limit: number): Promise<string[]>
  sent(ids: readonly string[]): Promise<string[]>
  forget(ids: readonly string[]): Promise<void>
  runEntries(flowRunId: string): Promise<RunEntryRow[]>
}

const fits = (id: string): boolean => id.length > 0 && id.length <= REQUEST_ID_MAX
/** At most the API's `IDS_MAX` a call. */
const IDS_MAX = 1000

/** EscalationRoutes over the API's CRUD routes, area `escalations`. */
export class CrudEscalations implements EscalationRoutes {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async entry(requestId: string): Promise<EscalationEntry | undefined> {
    if (!fits(requestId)) return undefined
    const { entry } = await this.#call<{ entry: EscalationEntry | null }>('an escalated call', `/escalations/entry${qs({ request_id: requestId })}`)
    return entry ?? undefined
  }

  async passUp(requestId: string, session: string, reason: string | undefined): Promise<boolean> {
    if (!fits(requestId) || !isUuid(session)) return false
    const { moved } = await this.#call<{ moved: boolean }>('a call passed up', '/escalations/pass-up', {
      body: wellFormed({ request_id: requestId, session, reason: reason ?? null }),
    })
    return moved
  }

  async claimDeny(requestId: string, session: string): Promise<Date | undefined> {
    if (!fits(requestId) || !isUuid(session)) return undefined
    const { hop_until } = await this.#call<{ hop_until: string | null }>("a deny's claim", '/escalations/claim-deny', {
      body: wellFormed({ request_id: requestId, session }),
    })
    return hop_until === null ? undefined : new Date(hop_until)
  }

  async releaseDeny(requestId: string, hopUntil: Date): Promise<void> {
    await this.#call("a deny's claim released", '/escalations/release-deny', { body: wellFormed({ request_id: requestId, hop_until: hopUntil.toISOString() }) })
  }

  async moveDue(): Promise<number> {
    const { moved } = await this.#call<{ moved: number }>('the parent hops past their time', '/escalations/move-due', { body: {} })
    return moved
  }

  async waiting(graceS: number): Promise<WaitingRow[]> {
    const { calls } = await this.#call<{ calls: (Omit<WaitingRow, 'hop_until'> & { hop_until: string })[] }>('the calls to deliver', '/escalations/waiting', {
      body: { grace_s: graceS },
    })
    return calls.map((c) => ({ ...c, hop_until: new Date(c.hop_until) }))
  }

  async claim(calls: SessionCalls): Promise<string[]> {
    if (!isUuid(calls.session)) return []
    const { claimed } = await this.#call<{ claimed: string[] }>('a delivery claimed', '/escalations/claim', { body: wellFormed(this.#calls(calls)) })
    return claimed
  }

  async unclaim(calls: SessionCalls): Promise<void> {
    if (!isUuid(calls.session)) return
    await this.#call('a delivery handed back', '/escalations/unclaim', { body: wellFormed(this.#calls(calls)) })
  }

  async skip(calls: SessionCalls): Promise<void> {
    if (!isUuid(calls.session)) return
    await this.#call('the calls a session cannot be asked about', '/escalations/skip', { body: wellFormed(this.#calls(calls)) })
  }

  async deliveries(after: string, limit: number): Promise<string[]> {
    const { ids } = await this.#call<{ ids: string[] }>('the deliveries recorded', '/escalations/deliveries/page', {
      body: wellFormed({ after, limit: Math.min(limit, IDS_MAX) }),
    })
    return ids
  }

  async sent(ids: readonly string[]): Promise<string[]> {
    const out: string[] = []
    for (const batch of batches(ids.filter(fits))) {
      const { ids: found } = await this.#call<{ ids: string[] }>('the calls already sent', '/escalations/deliveries/sent', { body: wellFormed({ ids: batch }) })
      out.push(...found)
    }
    return out
  }

  async forget(ids: readonly string[]): Promise<void> {
    for (const batch of batches(ids.filter(fits))) {
      await this.#call('the deliveries forgotten', '/escalations/deliveries/forget', { body: wellFormed({ ids: batch }) })
    }
  }

  async runEntries(flowRunId: string): Promise<RunEntryRow[]> {
    const { entries } = await this.#call<{ entries: (Omit<RunEntryRow, 'created_at' | 'expires_at'> & { created_at: string; expires_at: string })[] }>(
      "a run's child entries",
      `/escalations/run-entries${qs({ flow_run_id: flowRunId })}`,
    )
    return entries.map((e) => ({ ...e, created_at: new Date(e.created_at), expires_at: new Date(e.expires_at) }))
  }

  #calls(calls: SessionCalls): { session: string; child: string[]; own: string[] } {
    return { session: calls.session, child: calls.child.filter(fits), own: calls.own.filter(fits) }
  }
}

function batches(ids: readonly string[]): string[][] {
  const out: string[][] = []
  for (let i = 0; i < ids.length; i += IDS_MAX) out.push(ids.slice(i, i + IDS_MAX))
  return out
}
