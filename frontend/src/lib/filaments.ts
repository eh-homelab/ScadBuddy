import type { FilamentWarning, SlotNeed, SpoolOption } from '../api/types'

/**
 * View-model helpers for the filament picker (#87). No React and no fetching: the
 * server already joined Bambuddy's spool inventory, its assignments and the printer's
 * live AMS state into one `FilamentOptions`, so everything here is a read of that.
 *
 * The one rule that runs through all of it: an unknown value is reported as unknown.
 * Bambuddy spells "unknown" as `0` for a plate it has not sliced and as `null` for a
 * spool it cannot weigh, and both of those are one keystroke away from reading as
 * "needs nothing" or "has nothing" — which would either hide every spool behind the
 * "enough for this print" filter or claim a 2 g remnant will do.
 */

/**
 * `"Bambu Lab PLA Silk — Blue"`.
 *
 * Brand, subtype and colour name are each nullable on the wire — a spool added by hand
 * carries only a material — so this joins whatever is actually there. The em dash is
 * ScadBuddy's; the backend's own warning text spells the same spool with a plain space,
 * and that text is quoted verbatim rather than rebuilt, so the two do differ by design.
 */
export function spoolLabel(spool: SpoolOption): string {
  const name = [spool.brand, spool.material, spool.subtype].filter(Boolean).join(' ')
  if (!spool.color_name) return name || `spool #${spool.spool_id}`
  return name ? `${name} — ${spool.color_name}` : spool.color_name
}

/**
 * Where a spool physically is, or `null` when it is on the shelf.
 *
 * Three things about Bambuddy's numbering are visible here. `tray_id` is the printer's
 * 0-based index while Bambu's own UI counts AMS slots from 1, so the human number wins.
 * The AMS-HT is `ams_id` 128 and holds a single spool, so it has no slot to name, and an
 * external spool is not in an AMS at all. `inlet` is the A/B the filament switcher has
 * this AMS on — on a two-extruder machine that is what decides whether a slot can reach
 * the spool, so it is shown on the row rather than left for a warning to explain.
 *
 * `printerId` is the printer the print is scoped to. A spool loaded somewhere else is
 * still a legitimate choice, and naming the other printer is the difference between
 * "load this" and a silent surprise — but repeating the chosen printer on every row
 * would say nothing, so it is named only when it differs.
 */
export function loadedLabel(spool: SpoolOption, printerId?: number | null): string | null {
  const loaded = spool.loaded
  if (!loaded) return null
  let where: string
  if (loaded.is_external) where = 'External spool'
  else if (loaded.is_ams_ht) where = 'AMS-HT'
  else where = `AMS ${loaded.ams_id} · slot ${loaded.tray_id + 1}`
  if (loaded.inlet) where += ` · inlet ${loaded.inlet}`
  if (printerId !== null && printerId !== undefined && loaded.printer_id !== printerId) {
    where += ` · on ${loaded.printer_name ?? 'another printer'}`
  }
  return where
}

/**
 * The grams one slot needs for `copies` copies, or `null` when nobody knows yet.
 *
 * `used_grams` is `0` for every slot of a plate Bambuddy has not sliced — that is what
 * `filament-requirements` answers for a fresh upload — so zero is UNKNOWN here, never
 * "needs nothing". Both spellings collapse to `null` so no caller can print "0 g", and
 * so the sufficiency rule declines to judge instead of declaring every spool adequate.
 */
export function slotNeed(slot: SlotNeed, copies: number): number | null {
  const used = slot.used_grams
  if (used === null || used === undefined || used <= 0) return null
  return used * copies
}

export interface SpoolFilters {
  /** Exact `material`, or `''` for any. */
  material: string
  subtype: string
  brand: string
  /** Free text over the colour name, brand, material, subtype and slicer preset name. */
  search: string
  loadedOnly: boolean
  enoughOnly: boolean
}

export const NO_FILTERS: SpoolFilters = {
  material: '',
  subtype: '',
  brand: '',
  search: '',
  loadedOnly: false,
  enoughOnly: false,
}

/**
 * The spools on offer for one slot. `need` is that slot's grams from `slotNeed`.
 *
 * "Enough for this print" is the filter that has to fail open. Grams are unknown for an
 * unsliced plate and remaining weight is unknown for an untagged spool (Bambuddy reports
 * `remain: -1`, which the backend normalises to `null`), and in either case there is
 * nothing to compare. Hiding a row on an unknown would empty the list for exactly the
 * common case — a plate that has not been sliced yet — and look like an inventory fault.
 */
export function filterSpools(
  spools: SpoolOption[],
  filters: SpoolFilters,
  need: number | null = null,
): SpoolOption[] {
  const needle = filters.search.trim().toLowerCase()
  return spools.filter((spool) => {
    if (filters.material && spool.material !== filters.material) return false
    if (filters.subtype && (spool.subtype ?? '') !== filters.subtype) return false
    if (filters.brand && (spool.brand ?? '') !== filters.brand) return false
    if (filters.loadedOnly && !spool.loaded) return false
    if (
      filters.enoughOnly &&
      need !== null &&
      spool.remaining_g !== null &&
      spool.remaining_g !== undefined &&
      spool.remaining_g < need
    ) {
      return false
    }
    if (needle) {
      const parts = [
        spool.color_name,
        spool.brand,
        spool.material,
        spool.subtype,
        spool.slicer_filament_name,
      ]
      if (!parts.some((part) => part?.toLowerCase().includes(needle))) return false
    }
    return true
  })
}

export interface Facets {
  materials: string[]
  subtypes: string[]
  brands: string[]
}

/** The distinct values behind the three filter selects, so nothing is offered that matches nothing. */
export function facets(spools: SpoolOption[]): Facets {
  const distinct = (values: (string | null | undefined)[]) =>
    [...new Set(values.filter((value): value is string => Boolean(value)))].sort((a, b) =>
      a.localeCompare(b),
    )
  return {
    materials: distinct(spools.map((spool) => spool.material)),
    subtypes: distinct(spools.map((spool) => spool.subtype)),
    brands: distinct(spools.map((spool) => spool.brand)),
  }
}

/** The warnings about one slot. A plate-wide warning carries `slot_id: null` and is not one. */
export function warningsFor(warnings: FilamentWarning[], slotId: number): FilamentWarning[] {
  return warnings.filter((warning) => warning.slot_id === slotId)
}

/**
 * Plate-wide warnings last.
 *
 * The temperature rules are about the plate as a whole — an empty intersection across
 * the spools sharing one extruder — so they arrive with `slot_id: null`. Left in the
 * server's order they would read as a remark on whichever slot happened to precede
 * them. The sort is stable, so within each band the server's order survives.
 */
export function sortWarnings(warnings: FilamentWarning[]): FilamentWarning[] {
  const rank = (warning: FilamentWarning) =>
    warning.slot_id === null || warning.slot_id === undefined ? 1 : 0
  return [...warnings].sort((a, b) => rank(a) - rank(b))
}
