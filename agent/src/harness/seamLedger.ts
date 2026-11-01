import type { RiskTier } from './permissions.js'

/** A call a person approved: its gate entry, and the input the approval was given for (the verdict's). */
export type CallApproval = { requestId: string; input: Record<string, unknown> }

/**
 * The tier the permission seam applied to each call, by its tool_use id (#2171, spec
 * §4.4): what the in-process server's #2100 check compares a call's tier now against,
 * so a call the seam parked by a tier raised mid-turn runs once approved. One turn's
 * calls only: sessions/turnRunner.ts gives each turn a fresh one and drops it with the
 * turn, so nothing here is ever pruned.
 */
export class SeamLedger {
  readonly #tiers = new Map<string, RiskTier>()
  readonly #approvals = new Map<string, CallApproval>()

  record(toolUseId: string, tier: RiskTier): void {
    this.#tiers.set(toolUseId, tier)
  }

  /** The gate entry a person approved this call on (plan 2026-10-13 Step 9.2: its ToolCall runs on it). */
  approve(toolUseId: string, approval: CallApproval): void {
    this.#approvals.set(toolUseId, approval)
  }

  /** The entry this call was approved on, and its input; undefined when it never parked, or was not approved. */
  approvalOf(toolUseId: string | undefined): CallApproval | undefined {
    return toolUseId === undefined ? undefined : this.#approvals.get(toolUseId)
  }

  /** The tier the seam applied to this call; undefined when it decided none (or there is no id). */
  tierOf(toolUseId: string | undefined): RiskTier | undefined {
    return toolUseId === undefined ? undefined : this.#tiers.get(toolUseId)
  }
}
