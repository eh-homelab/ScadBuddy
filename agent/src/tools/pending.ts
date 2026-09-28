import { randomUUID } from 'node:crypto'
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { MAX_APPROVAL_EXPIRY_SECONDS } from '../approvals/service.js'
import type { ToolContext } from './registry.js'

// Pending outward actions, the `prepare` half of spec §8.2's two-step flow for
// external MCP clients: an outward tool call records what it would do and
// returns an id and a human-readable summary instead of doing it.
//
// This store holds what the call would RUN (the tool and its full arguments),
// in memory. The approval itself (who asked, the decision, the input hash and
// its single use) is a row in `ai_approvals` with the same id, recorded by
// runTool through `ToolServices.approvals` (registry.ts, approvals/service.ts),
// so a human decides it in the ScadBuddy UI like a session's. `confirm_action`
// (approvals.ts) needs both: the row says whether it may run, this says what.
// The arguments stay out of the table on purpose: ai_approvals keeps a
// scrubbed summary and an HMAC of the input, never the input
// (db/migrations/20260928T0734Z_approvals.sql). So a pending action dies with
// the process, which is the safe direction: after a restart, or on another
// replica, an approved row has nothing to run and the caller prepares again.

export type PendingAction = {
  readonly id: string
  readonly tool: string
  readonly args: unknown
  readonly summary: string
  readonly principalId: string
  /** Runs the prepared call (the tool's handler, past the gate); set by runTool. */
  readonly run?: (ctx: ToolContext) => Promise<CallToolResult>
  readonly createdAt: Date
  readonly expiresAt: Date
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

export class PendingStoreFullError extends Error {
  override name = 'PendingStoreFullError'
}

export class PendingActionStore {
  readonly #actions = new Map<string, PendingAction>()
  readonly #ttlMs: number
  readonly #perPrincipal: number
  readonly #total: number

  constructor(options: PendingLimits = {}) {
    // As long as its approval can matter: the longest wait for a decision
    // plus the longest an approved one stays usable (approvals/service.ts).
    this.#ttlMs = options.ttlMs ?? 2 * MAX_APPROVAL_EXPIRY_SECONDS * 1000
    this.#perPrincipal = options.perPrincipal ?? 50
    this.#total = options.total ?? 10_000
  }

  prepare(input: {
    tool: string
    args: unknown
    summary: string
    principalId: string
    run?: (ctx: ToolContext) => Promise<CallToolResult>
  }): PendingAction {
    this.#sweep()
    // Map iteration order is insertion order, so this list is oldest first.
    const own = [...this.#actions.values()].filter((a) => a.principalId === input.principalId)
    if (own.length >= this.#perPrincipal) {
      for (const stale of own.slice(0, own.length - this.#perPrincipal + 1)) this.#actions.delete(stale.id)
    } else if (this.#actions.size >= this.#total) {
      throw new PendingStoreFullError(
        'too many actions are waiting for approval right now; try again once some are approved or expire',
      )
    }
    const now = new Date()
    const action: PendingAction = {
      ...input,
      id: randomUUID(),
      createdAt: now,
      expiresAt: new Date(now.getTime() + this.#ttlMs),
    }
    this.#actions.set(action.id, action)
    return action
  }

  /** The action, only for the principal that prepared it and only until it expires. */
  get(id: string, principalId: string): PendingAction | undefined {
    this.#sweep()
    const action = this.#actions.get(id)
    return action && action.principalId === principalId ? action : undefined
  }

  /** Forgets an action: once it ran, or once its approval can no longer let it run. */
  remove(id: string): void {
    this.#actions.delete(id)
  }

  list(principalId: string): PendingAction[] {
    this.#sweep()
    return [...this.#actions.values()].filter((a) => a.principalId === principalId)
  }

  #sweep(now = Date.now()): void {
    for (const [id, action] of this.#actions) {
      if (action.expiresAt.getTime() <= now) this.#actions.delete(id)
    }
  }
}
