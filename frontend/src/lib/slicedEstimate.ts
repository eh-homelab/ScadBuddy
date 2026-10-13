import type { FilamentOptions, SlicePreview, SlicedProfile } from '../api/types'

/**
 * #2671 — the preview pane's summary for a sliced file: Bambu Studio's own estimate for
 * the plate, in place of a background slice, which a sliced file never gets.
 */
export function slicedEstimate(
  profile: SlicedProfile,
  plate: number,
  filaments: FilamentOptions | null,
): SlicePreview | null {
  const sliced = (profile.plates ?? []).find((entry) => entry.index === plate)
  if (!sliced) return null
  return {
    job_id: 0,
    status: 'completed',
    print_time_seconds: sliced.print_seconds ?? null,
    filament_used_g: sliced.weight_g ?? null,
    slots: (filaments?.slots ?? [])
      .filter((slot) => slot.used_grams != null)
      .map((slot) => ({ slot_id: slot.slot_id, grams: slot.used_grams ?? null })),
  }
}
