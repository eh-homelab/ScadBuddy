import type { SlotChoice, SlotNeed, SpoolOption } from '../api/types'
import { normalizeHex } from './format'
import { parseRgba } from './rgba'

/**
 * `#RRGGBBAA` when the colour spells an alpha, FF included, else `#RRGGBB`: the 3D preview
 * draws a spelled alpha, and a colour with none keeps the design part's own opacity (#6349).
 */
function previewColour(colour: string): string {
  const rgba = parseRgba(colour)
  return rgba !== null && /^#?[0-9A-Fa-f]{8}$/.test(colour) ? rgba : normalizeHex(colour)
}

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
    const chosen = spoolId == null ? undefined : byId.get(spoolId)
    // `rgba` spells the alpha; `colour` is `#RRGGBB` (the backend keeps it six digits).
    const colour = chosen?.rgba ?? chosen?.colour
    // The first slot to claim a design colour wins: two slots of one colour are one part.
    const design = normalizeHex(slot.colour)
    if (colour && !out.has(design)) out.set(design, previewColour(colour))
  }
  return out
}
