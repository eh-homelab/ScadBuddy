import type { SlotChoice, SlotNeed, SpoolOption } from '../api/types'
import { normalizeHex } from './format'

/**
 * #1723 — what each part of the plate prints in: its slot's design colour → the colour of
 * the spool the dialog has chosen for that slot. A slot with no chosen spool, or a spool
 * with no colour, keeps its design colour (it is left out of the map).
 */
export function resolvedColors(
  slots: readonly SlotNeed[],
  spools: readonly SpoolOption[],
  plan: readonly SlotChoice[],
): Map<string, string> {
  const byId = new Map(spools.map((spool) => [spool.spool_id, spool]))
  const out = new Map<string, string>()
  for (const slot of slots) {
    if (!slot.colour) continue
    const spoolId = plan.find((choice) => choice.slot_id === slot.slot_id)?.spool_id
    const colour = spoolId == null ? null : byId.get(spoolId)?.colour
    // The first slot to claim a design colour wins: two slots of one colour are one part.
    const design = normalizeHex(slot.colour)
    if (colour && !out.has(design)) out.set(design, normalizeHex(colour))
  }
  return out
}
