import { createHash, randomUUID } from 'node:crypto'
import type { Principal } from '../auth/principal.js'
import type { Owner } from '../sessions/protocol.js'
import {
  type Claim,
  DEFAULT_PENDING_PER_PRINCIPAL,
  DEFAULT_PENDING_TOTAL,
  type OutwardActions,
  PENDING_STORE_FULL,
  type PrepareCall,
  type PreparedAction,
  PendingStoreFullError,
} from '../tools/pending.js'
import { ApprovalError, type ApprovalRecord, type ApprovalService } from './service.js'

// Spec §8.2's prepare/confirm for external MCP clients, on the approval store
// (#258): "a two-step `prepare` (returns a pending action id and a
// human-readable summary) then `confirm`, where the confirm completes only
// after the UI approval".
//
//   prepare  an outward tool called over /mcp → `ApprovalService.create()`
//            with no session and no turn, requested by the MCP principal:
//            the row carries the tool, the HMAC input hash and the scrubbed
//            input summary. The pending approval's id is the pending action id.
//   decide   the UI, through the approval routes (routes/approvals.ts) as the
//            browser user. `authorize` refuses a principal deciding its own
//            request, so an MCP client cannot approve what it prepared.
//   confirm  `claim()` → `consumePrepared()`: approved, unused, not voided,
//            within `usable_until`, the same principal, the same input hash;
//            one UPDATE, so the call runs exactly once.
//
// The full input is never stored (the table holds the hash and a scrubbed
// summary), so `confirm_action` takes the arguments again and they must hash
// to the approved value: a changed input needs a new approval.

/**
 * The principal as an approval's `requested_by`. An anonymous principal's id
 * is `anonymous:<Mcp-Session-Id>`, and the session id is that client's
 * capability (mcp/http.ts), so only a digest of it is stored and shown.
 */
export function ownerOf(principal: Principal): Owner {
  switch (principal.kind) {
    case 'anonymous': {
      const digest = createHash('sha256').update(principal.id, 'utf8').digest('hex').slice(0, 32)
      return {
        kind: 'anonymous',
        id: `anonymous:${digest}`,
        label: `anonymous MCP client${principal.clientIp ? ` (${principal.clientIp})` : ''}`,
      }
    }
    case 'bearer':
      return { kind: 'bearer', id: principal.id, label: `MCP ${principal.id}` }
    case 'oidc': {
      // The id is `oidc:<issuer>#<sub>` (auth/oidc.ts); the label names the
      // subject and, when the token says, the OAuth client it was issued to.
      const subject = principal.subject ?? principal.id
      const client = principal.clientId ? ` via ${principal.clientId}` : ''
      return { kind: 'oidc', id: principal.id, label: `MCP OIDC ${subject}${client}` }
    }
    case 'browser':
      return { kind: 'browser', id: principal.id, label: 'You' }
    default: {
      // A new PrincipalKind must be mapped here, never fall through to the browser user.
      const unknown: never = principal.kind
      throw new Error(`unknown principal kind ${String(unknown)}`)
    }
  }
}

function asAction(a: ApprovalRecord, summary = `${a.tool} ${a.inputSummary}`): PreparedAction {
  return { id: a.id, tool: a.tool, summary, expiresAt: new Date(a.expiresAt) }
}

export class ApprovalActions implements OutwardActions {
  readonly #approvals: ApprovalService
  readonly #perPrincipal: number
  readonly #total: number

  constructor(approvals: ApprovalService, limits: { perPrincipal?: number; total?: number } = {}) {
    this.#approvals = approvals
    this.#perPrincipal = limits.perPrincipal ?? DEFAULT_PENDING_PER_PRINCIPAL
    this.#total = limits.total ?? DEFAULT_PENDING_TOTAL
  }

  async prepare(principal: Principal, call: PrepareCall): Promise<PreparedAction> {
    // The same bounds as the in-memory store, checked and applied in the
    // insert's own transaction (ApprovalService.createPrepared): a full queue
    // cancels the caller's own oldest; a full table refuses rather than evict anyone's.
    const approval = await this.#approvals.createPrepared(
      {
        // No SDK tool_use block exists for an MCP call; the column wants an id.
        toolUseId: `mcp:${randomUUID()}`,
        tool: call.tool,
        input: call.input,
        tier: 'outward',
        requestedBy: ownerOf(principal),
      },
      { perPrincipal: this.#perPrincipal, total: this.#total, evictReason: 'superseded: the caller prepared too many actions' },
    )
    if (!approval) throw new PendingStoreFullError(PENDING_STORE_FULL)
    return asAction(approval, call.summary)
  }

  async list(principal: Principal): Promise<PreparedAction[]> {
    return (await this.#approvals.listPrepared(ownerOf(principal))).map((a) => asAction(a))
  }

  /** The record, only when this principal prepared it over MCP. */
  async #record(id: string, principal: Principal): Promise<ApprovalRecord | undefined> {
    const by = ownerOf(principal)
    try {
      const approval = await this.#approvals.get(id, by)
      // `get` lets the browser user and grant holders see others' approvals;
      // a pending action is only ever its preparer's.
      const own = approval.requestedBy.kind === by.kind && approval.requestedBy.id === by.id
      return own && approval.sessionId === null ? approval : undefined
    } catch (err) {
      if (err instanceof ApprovalError && err.code === 'not_found') return undefined
      throw err
    }
  }

  async find(id: string, principal: Principal): Promise<PreparedAction | undefined> {
    const approval = await this.#record(id, principal)
    return approval ? asAction(approval) : undefined
  }

  async claim(id: string, principal: Principal, input: Record<string, unknown>): Promise<Claim> {
    await this.#approvals.expireIfDue(id)
    const approval = await this.#record(id, principal)
    if (!approval) return { status: 'refused', reason: `no pending action ${id} for this caller` }
    const what = `"${approval.tool}" (pending action ${id})`
    switch (approval.decision) {
      case null:
        return { status: 'pending', action: asAction(approval) }
      case 'denied':
        return {
          status: 'refused',
          reason: `The user denied ${what} in the ScadBuddy UI; nothing was sent. Do not retry it unless the user asks.`,
        }
      case 'expired':
        return { status: 'refused', reason: `Nobody approved ${what} before it expired; nothing was sent. Prepare it again.` }
      case 'cancelled':
        return {
          status: 'refused',
          reason: `${what} was cancelled (${approval.reason ?? 'no reason given'}); nothing was sent.`,
        }
      case 'approved':
        break
    }
    if (approval.consumedAt !== null) {
      return { status: 'refused', reason: `${what} was already confirmed; an approval runs its action once. Nothing was sent.` }
    }
    if (approval.revokedAt !== null) {
      return { status: 'refused', reason: `The approval of ${what} was withdrawn (${approval.reason ?? 'no reason given'}); nothing was sent.` }
    }
    if (this.#approvals.hash(approval.tool, input) !== approval.inputHash) {
      return {
        status: 'refused',
        reason:
          `These arguments are not the ones ${what} was prepared and approved with; nothing was sent. ` +
          'Confirm with exactly the same arguments, or call the tool again for a new approval.',
      }
    }
    const used = await this.#approvals.consumePrepared(id, ownerOf(principal), approval.inputHash)
    if (!used) {
      return {
        status: 'refused',
        reason: `The approval of ${what} is no longer usable (already used, withdrawn or out of time); nothing was sent.`,
      }
    }
    return { status: 'approved', action: asAction(used) }
  }
}
