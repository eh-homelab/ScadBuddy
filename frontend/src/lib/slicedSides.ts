import type { NozzlePlan, SlicePreview } from '../api/types'

/**
 * #2703 — the plan with each filament on the side the slice actually put it, or `null`
 * when the slice agrees with the plan on every one of its slots (or says nothing). With
 * every side on Automatic the headless slicer groups filaments itself to cut purging and
 * does not follow the plan's project-level Manual `filament_map` (measured for #745); a
 * side chosen by hand pins the map on every plate, which it does follow (#3955). Either
 * way, once a slice of these choices has landed, it is what will print.
 */
export function asSliced(plan: NozzlePlan | null | undefined, slice: SlicePreview | null): NozzlePlan | null {
  if (!plan || !slice) return null
  // A slice that names no side says nothing of where anything went.
  if (!(slice.slots ?? []).some((entry) => entry.side)) return null
  const sides = new Map((slice.slots ?? []).map((entry) => [entry.slot_id, entry.side]))
  let moved = false
  const slots = (plan.slots ?? [])
    // A slot this plate's slice does not use has no sliced side; its planned one is not
    // shown on a line that says "as sliced" (#2737 review). So the line's flow words name
    // only the sides this plate prints from, which is what "as sliced" means (#3595 review).
    .filter((slot) => sides.has(slot.slot_id))
    .map((slot) => {
      const side = sides.get(slot.slot_id)
      if (!side || side === slot.side) return slot
      moved = true
      return { ...slot, side }
    })
  // The slice's copy whenever it left a slot off or moved one (#3751), unless nothing of
  // the plan's is left, when the plan's line stays. Its summary is empty: the plan's names
  // the planned slots and sides, and none is better than a wrong one.
  if (slots.length === 0) return null
  const filtered = slots.length < (plan.slots ?? []).length
  return moved || filtered ? { ...plan, slots, summary: '' } : null
}
