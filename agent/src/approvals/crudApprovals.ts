import { wellFormed } from '../api/crudRead.js'
import { type CrudTarget, crudOf, qs } from '../api/crudStores.js'
import type { Logged } from '../gate/crudStore.js'
import type { Owner } from '../sessions/protocol.js'
import type { ApprovalRow } from './service.js'

// The classic approvals through the API's CRUD routes (#5299 step 10; backend
// api/crud_agent_approvals.py, agent_data/approvals.py), area `approvals`. ApprovalService
// (approvals/service.ts) calls these once the API lists the area, and its own SQL until then (D9).
// Who may see or decide an approval stays the service's check; a decision is written on the API
// once, with its span's context, its session events and its audit row in one transaction.

/** A row as the API answers it: its times as ISO strings. */
type WireRow = Omit<ApprovalRow, 'created_at' | 'expires_at' | 'decided_at' | 'usable_until' | 'consumed_at' | 'revoked_at'> & {
  created_at: string
  expires_at: string
  decided_at: string | null
  usable_until: string | null
  consumed_at: string | null
  revoked_at: string | null
}

const at = (iso: string | null): Date | null => (iso === null ? null : new Date(iso))

/** A row from the API, its times parsed. */
export function rowOf(w: WireRow): ApprovalRow {
  return {
    ...w,
    created_at: new Date(w.created_at),
    expires_at: new Date(w.expires_at),
    decided_at: at(w.decided_at),
    usable_until: at(w.usable_until),
    consumed_at: at(w.consumed_at),
    revoked_at: at(w.revoked_at),
  }
}

/** The session access an approval's checks take. */
export type SessionAccessRow = {
  owner_kind: Owner['kind']
  owner_id: string
  owner_label: string
  creator_kind: Owner['kind']
  creator_id: string
  offer_kind: Owner['kind'] | null
  offer_id: string | null
}

/** A new pending approval's row, its summary and hash made here. */
export type NewApprovalRow = {
  id: string
  session_id: string | null
  turn_id: string | null
  tool_use_id: string
  tool: string
  input_summary: string
  input_hash: string
  tier: string
  requested_by: { kind: string; id: string; label: string }
  requested_tiers: string[] | null
  expiry_s: number
  traceparent: string | null
}

export type SettleCall = {
  id: string
  decision: 'approved' | 'denied' | 'expired' | 'cancelled'
  by: Owner | undefined
  reason: string | null
  expiryS: number
  traceparent: string | undefined
  surface?: string | undefined
  clientIp?: string | undefined
}

/** The approvals' routes, as ApprovalService calls them. */
export interface ApprovalRoutes {
  row(id: string): Promise<ApprovalRow | undefined>
  session(id: string): Promise<SessionAccessRow | undefined>
  visiblePending(who: Pick<Owner, 'kind' | 'id'>): Promise<ApprovalRow[]>
  list(sessionId: string | undefined, pending: boolean): Promise<ApprovalRow[]>
  prepared(who: Pick<Owner, 'kind' | 'id'>): Promise<ApprovalRow[]>
  due(tried: readonly string[], limit: number): Promise<string[]>
  create(row: NewApprovalRow): Promise<{ approval: ApprovalRow; logged: Logged[] }>
  createPrepared(
    row: NewApprovalRow,
    bounds: { perPrincipal: number; total: number; evictReason: string },
    evictTraceparents: readonly string[],
  ): Promise<{ approval: ApprovalRow | undefined; evicted: ApprovalRow[] }>
  settle(call: SettleCall): Promise<{ approval: ApprovalRow | undefined; logged: Logged[] }>
  consume(id: string, by: Pick<Owner, 'kind' | 'id'>, inputHash: string): Promise<ApprovalRow | undefined>
}

const who = (o: Pick<Owner, 'kind' | 'id'>) => ({ kind: o.kind, id: o.id })

/** ApprovalRoutes over the API's CRUD routes, area `approvals`. */
export class CrudApprovals implements ApprovalRoutes {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async row(id: string): Promise<ApprovalRow | undefined> {
    const { approval } = await this.#call<{ approval: WireRow | null }>('an approval', `/approvals/row${qs({ id })}`)
    return approval ? rowOf(approval) : undefined
  }

  async session(id: string): Promise<SessionAccessRow | undefined> {
    const { session } = await this.#call<{ session: SessionAccessRow | null }>("an approval's session", `/approvals/session${qs({ session_id: id })}`)
    return session ?? undefined
  }

  async #rows(step: string, path: string, body: unknown): Promise<ApprovalRow[]> {
    const { approvals } = await this.#call<{ approvals: WireRow[] }>(step, path, { body: wellFormed(body) })
    return approvals.map(rowOf)
  }

  async visiblePending(by: Pick<Owner, 'kind' | 'id'>): Promise<ApprovalRow[]> {
    return await this.#rows('the visible approvals', '/approvals/visible-pending', who(by))
  }

  async list(sessionId: string | undefined, pending: boolean): Promise<ApprovalRow[]> {
    return await this.#rows('the approvals', '/approvals/list', { session_id: sessionId ?? null, pending })
  }

  async prepared(by: Pick<Owner, 'kind' | 'id'>): Promise<ApprovalRow[]> {
    return await this.#rows('the prepared actions', '/approvals/prepared', who(by))
  }

  async due(tried: readonly string[], limit: number): Promise<string[]> {
    const { ids } = await this.#call<{ ids: string[] }>('the approvals due', '/approvals/due', { body: wellFormed({ tried: [...tried], limit }) })
    return ids
  }

  async create(row: NewApprovalRow): Promise<{ approval: ApprovalRow; logged: Logged[] }> {
    const made = await this.#call<{ approval: WireRow; logged: Logged[] }>('an approval', '/approvals/create', { body: wellFormed(row) })
    return { approval: rowOf(made.approval), logged: made.logged }
  }

  async createPrepared(
    row: NewApprovalRow,
    bounds: { perPrincipal: number; total: number; evictReason: string },
    evictTraceparents: readonly string[],
  ): Promise<{ approval: ApprovalRow | undefined; evicted: ApprovalRow[] }> {
    const body = {
      approval: row,
      per_principal: bounds.perPrincipal,
      total: bounds.total,
      evict_reason: bounds.evictReason,
      evict_traceparents: [...evictTraceparents],
    }
    const made = await this.#call<{ approval: WireRow | null; evicted: WireRow[] }>('a prepared action', '/approvals/create-prepared', { body: wellFormed(body) })
    return { approval: made.approval ? rowOf(made.approval) : undefined, evicted: made.evicted.map(rowOf) }
  }

  async settle(call: SettleCall): Promise<{ approval: ApprovalRow | undefined; logged: Logged[] }> {
    const body = {
      id: call.id,
      decision: call.decision,
      by: call.by ? { kind: call.by.kind, id: call.by.id, label: call.by.label } : null,
      reason: call.reason,
      expiry_s: call.expiryS,
      traceparent: call.traceparent ?? null,
      surface: call.surface ?? null,
      client_ip: call.clientIp ?? null,
    }
    const settled = await this.#call<{ approval: WireRow | null; logged: Logged[] }>('a decision', '/approvals/settle', { body: wellFormed(body) })
    return { approval: settled.approval ? rowOf(settled.approval) : undefined, logged: settled.logged }
  }

  async consume(id: string, by: Pick<Owner, 'kind' | 'id'>, inputHash: string): Promise<ApprovalRow | undefined> {
    const { approval } = await this.#call<{ approval: WireRow | null }>('a prepared action used', '/approvals/consume', {
      body: wellFormed({ id, by: who(by), input_hash: inputHash }),
    })
    return approval ? rowOf(approval) : undefined
  }
}
