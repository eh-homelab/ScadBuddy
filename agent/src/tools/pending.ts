import { randomUUID } from 'node:crypto'
import type { Principal } from '../auth/principal.js'

// Pending outward actions: spec §8.2's two-step flow for external MCP clients.
// An outward tool call (the `prepare`) records what it would do and returns
// an id and a human-readable summary instead of doing it; `confirm_action`
// (tools/approvals.ts) runs it only once a human has approved it in the
// ScadBuddy UI.
//
// Two stores implement `OutwardActions`:
//   - approvals/mcp.ts `ApprovalActions`, over `ai_approvals` (#258): the one
//     main.ts uses whenever there is a database. The UI decides there, and a
//     confirm runs the call once, for the principal that prepared it, with the
//     input that was approved.
//   - `PendingActionStore` below, in memory, when there is no database: with
//     nowhere for a human to approve, its actions are never confirmed. The
//     safe direction: nothing outward runs without the approval store.

export type PreparedAction = {
  readonly id: string
  readonly tool: string
  readonly summary: string
  readonly expiresAt: Date
}

/** What a confirm found. Only `approved` runs the call, and it has used the approval up. */
export type Claim =
  | { status: 'approved'; action: PreparedAction }
  | { status: 'pending'; action: PreparedAction }
  | { status: 'refused'; reason: string }

export type PrepareCall = {
  tool: string
  /** The parsed arguments (defaults applied): what an approval binds to. */
  input: Record<string, unknown>
  /** The tool's own human-readable line. */
  summary: string
}

export interface OutwardActions {
  prepare(principal: Principal, call: PrepareCall): Promise<PreparedAction>
  /** The caller's own actions still waiting for a decision, oldest first. */
  list(principal: Principal): Promise<PreparedAction[]>
  /** The action, only for the principal that prepared it. */
  find(id: string, principal: Principal): Promise<PreparedAction | undefined>
  /**
   * Uses an approved action once, for the principal that prepared it and the
   * exact input that was approved; otherwise says why not.
   */
  claim(id: string, principal: Principal, input: Record<string, unknown>): Promise<Claim>
}

/**
 * Bounds, so no caller can crowd out another's approvals:
 *
 * - `perPrincipal`: a caller's own queue. When it is full, THAT caller's
 *   oldest pending action is evicted; nobody else's ever is.
 * - `total`: a process-wide backstop against many principals at once (every
 *   anonymous session is its own principal in `disabled` mode). When it is
 *   reached, new prepares are REFUSED rather than evicting anyone's action.
 */
export type PendingLimits = { ttlMs?: number; perPrincipal?: number; total?: number }

export const DEFAULT_PENDING_PER_PRINCIPAL = 50
export const DEFAULT_PENDING_TOTAL = 10_000

export class PendingStoreFullError extends Error {
  override name = 'PendingStoreFullError'
}

export const PENDING_STORE_FULL =
  'too many actions are waiting for approval right now; try again once some are approved or expire'

// `expiresAt` is what callers are shown; `dueAt` (performance.now) is what expires it,
// so a wall-clock step neither ends nor stretches the TTL (#1485).
type MemoryAction = PreparedAction & { principalId: string; dueAt: number }

/** The no-database store: prepares and lists, and never confirms. */
export class PendingActionStore implements OutwardActions {
  readonly #actions = new Map<string, MemoryAction>()
  readonly #ttlMs: number
  readonly #perPrincipal: number
  readonly #total: number

  constructor(options: PendingLimits = {}) {
    this.#ttlMs = options.ttlMs ?? 15 * 60_000
    this.#perPrincipal = options.perPrincipal ?? DEFAULT_PENDING_PER_PRINCIPAL
    this.#total = options.total ?? DEFAULT_PENDING_TOTAL
  }

  async prepare(principal: Principal, call: PrepareCall): Promise<PreparedAction> {
    this.#sweep()
    // Map iteration order is insertion order, so this list is oldest first.
    const own = [...this.#actions.values()].filter((a) => a.principalId === principal.id)
    if (own.length >= this.#perPrincipal) {
      for (const stale of own.slice(0, own.length - this.#perPrincipal + 1)) this.#actions.delete(stale.id)
    } else if (this.#actions.size >= this.#total) {
      throw new PendingStoreFullError(PENDING_STORE_FULL)
    }
    const action: MemoryAction = {
      id: randomUUID(),
      tool: call.tool,
      summary: call.summary,
      principalId: principal.id,
      expiresAt: new Date(Date.now() + this.#ttlMs),
      dueAt: performance.now() + this.#ttlMs,
    }
    this.#actions.set(action.id, action)
    return strip(action)
  }

  async find(id: string, principal: Principal): Promise<PreparedAction | undefined> {
    this.#sweep()
    const action = this.#actions.get(id)
    return action && action.principalId === principal.id ? strip(action) : undefined
  }

  async list(principal: Principal): Promise<PreparedAction[]> {
    this.#sweep()
    return [...this.#actions.values()].filter((a) => a.principalId === principal.id).map(strip)
  }

  async claim(id: string, principal: Principal): Promise<Claim> {
    const action = await this.find(id, principal)
    if (!action) return { status: 'refused', reason: `no pending action ${id} for this caller (it may have expired)` }
    return {
      status: 'refused',
      reason:
        `Not confirmed: "${action.summary}" needs a human approval in the ScadBuddy UI, and approvals need the ` +
        "agent's database (SCADBUDDY_DATABASE_URL), which is not configured. Nothing was sent.",
    }
  }

  #sweep(now = performance.now()): void {
    for (const [id, action] of this.#actions) {
      if (action.dueAt <= now) this.#actions.delete(id)
    }
  }
}

function strip({ principalId: _p, dueAt: _d, ...action }: MemoryAction): PreparedAction {
  return action
}
