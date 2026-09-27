import { randomUUID } from 'node:crypto'

// Pending outward actions, the `prepare` half of spec §8.2's two-step flow for
// external MCP clients: an outward tool call records what it would do and
// returns an id and a human-readable summary instead of doing it.
//
// The `confirm` half completes only after a human approves in the ScadBuddy
// UI. That UI and its approval records are #258; until they exist
// `confirm_action` always refuses, so no outward action runs from an agent.
// In memory for now: pending actions die with the process, which is the safe
// direction (nothing can be approved that was not shown in this process).

export type PendingAction = {
  readonly id: string
  readonly tool: string
  readonly args: unknown
  readonly summary: string
  readonly principalId: string
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
    this.#ttlMs = options.ttlMs ?? 15 * 60_000
    this.#perPrincipal = options.perPrincipal ?? 50
    this.#total = options.total ?? 10_000
  }

  prepare(input: { tool: string; args: unknown; summary: string; principalId: string }): PendingAction {
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
