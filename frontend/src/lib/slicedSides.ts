import type { NozzlePlan, SlicePreview } from '../api/types'

/**
 * #2703 — the plan with each filament on the side the slice actually put it, or `null`
 * when the slice agrees with the plan (or says nothing). The headless slicer groups
 * filaments itself to cut purging and does not follow the plan's Manual `filament_map`
 * (measured for #745: a full-length Manual map changed nothing), so once a slice of
 * these choices has landed, it is what will print.
 */
export function asSliced(plan: NozzlePlan | null | undefined, slice: SlicePreview | null): NozzlePlan | null {
  if (!plan || !slice) return null
  const sides = new Map((slice.slots ?? []).map((entry) => [entry.slot_id, entry.side]))
  let moved = false
  const slots = (plan.slots ?? []).map((slot) => {
    const side = sides.get(slot.slot_id)
    if (!side || side === slot.side) return slot
    moved = true
    return { ...slot, side }
  })
  return moved ? { ...plan, slots } : null
}
