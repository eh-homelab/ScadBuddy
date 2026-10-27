import type { RiskTier } from './permissions.js'

/**
 * The tier the permission seam applied to each call, by its tool_use id (#2171, spec
 * §4.4): what the in-process server's #2100 check compares a call's tier now against,
 * so a call the seam parked by a tier raised mid-turn runs once approved. One turn's
 * calls only: sessions/turnRunner.ts gives each turn a fresh one and drops it with the
 * turn, so nothing here is ever pruned.
 */
export class SeamLedger {
  readonly #tiers = new Map<string, RiskTier>()

  record(toolUseId: string, tier: RiskTier): void {
    this.#tiers.set(toolUseId, tier)
  }

  /** The tier the seam applied to this call; undefined when it decided none (or there is no id). */
  tierOf(toolUseId: string | undefined): RiskTier | undefined {
    return toolUseId === undefined ? undefined : this.#tiers.get(toolUseId)
  }
}
