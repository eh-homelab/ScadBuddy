import type {
  FilamentOptions,
  FilamentWarning,
  SlotChoice,
  SlotNeed,
  SpoolOption,
} from '../api/types'

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
 `tray_id` is the printer's 0-based index while Bambu's own UI counts AMS slots from
 * 1, so the human number wins. This is a label and nothing more: which tray the print
 * actually draws from is Bambuddy's to decide at dispatch.
 *
 * `printerId` is the printer the print is scoped to. A spool loaded somewhere else is
 * still a legitimate choice, and naming the other printer is the difference between
 * "load this" and a silent surprise — but repeating the chosen printer on every row
 * would say nothing, so it is named only when it differs.
 */
export function loadedLabel(spool: SpoolOption, printerId?: number | null): string | null {
  const loaded = spool.loaded
  if (!loaded) return null
  let where = `AMS ${loaded.ams_id} · slot ${loaded.tray_id + 1}`
  if (printerId !== null && printerId !== undefined && loaded.printer_id !== printerId) {
    where += ` · on ${loaded.printer_name ?? 'another printer'}`
  }
  return where
}

type Hardware = Partial<Pick<FilamentOptions, 'nozzles' | 'track_switch'>>

/**
 * The extruders — 0 right, 1 left, as `nozzles` is indexed — whose fitted nozzle is
 * `size` (#469). With none the run is refused; with one of two reported, so is a
 * multi-color print.
 */
export function fittingSides(options: Hardware, size: string | undefined): (0 | 1)[] {
  const nozzles = options.nozzles ?? []
  return ([0, 1] as const).filter((extruder) => size && nozzles[extruder]?.nozzle_diameter === size)
}

/**
 * Why a spool cannot print at `size`, as a badge reads (`"L · 0.4 fitted"`), or `null`
 * when it can or nobody knows (#469).
 *
 * Only without the Filament Track Switch: there each AMS is wired to one side, so a
 * spool on the side with another nozzle fitted can't print. With the switch any AMS
 * reaches either nozzle, and a spool's side is only where it rests. An unknown side or
 * an unreported nozzle is not a mismatch.
 */
export function nozzleMismatch(
  spool: SpoolOption,
  options: Hardware,
  size: string | undefined,
): string | null {
  if (options.track_switch) return null
  const extruder = spool.extruder
  if (extruder === null || extruder === undefined || !size || !spool.side) return null
  const fitted = (options.nozzles ?? [])[extruder]?.nozzle_diameter
  if (!fitted || fitted === size) return null
  return `${spool.side} · ${fitted} fitted`
}

function rgb(hex: string | null | undefined): [number, number, number] | null {
  const text = (hex ?? '').replace('#', '')
  if (!/^[0-9a-fA-F]{6}/.test(text)) return null
  return [0, 2, 4].map((at) => parseInt(text.slice(at, at + 2), 16)) as [number, number, number]
}

/** The backend's `COLOUR_MATCH_DISTANCE`: "recognisably the same colour". */
const COLOUR_MATCH_DISTANCE = 48

/**
 * `plan` with every spool that can't print at `size` swapped for the closest one that
 * can — same material, a recognisably similar color, not used by another slot — or
 * dropped when there is none (review #6). A plan that opened on a spool the run would
 * refuse only fails on Print; an empty slot says "no filament chosen" up front.
 * Returns `plan` itself when nothing changes.
 */
export function fitPlan(options: FilamentOptions, plan: SlotChoice[], size: string): SlotChoice[] {
  const spools = options.spools ?? []
  const byId = new Map(spools.map((spool) => [spool.spool_id, spool]))
  const ruledOut = (spool: SpoolOption | undefined) =>
    spool !== undefined && nozzleMismatch(spool, options, size) !== null
  if (!plan.some((choice) => ruledOut(byId.get(choice.spool_id)))) return plan

  const taken = new Set(plan.map((choice) => choice.spool_id))
  return plan.flatMap((choice) => {
    if (!ruledOut(byId.get(choice.spool_id))) return [choice]
    const slot = (options.slots ?? []).find((entry) => entry.slot_id === choice.slot_id)
    const want = rgb(slot?.colour)
    let best: { distance: number; spool: SpoolOption } | null = null
    for (const spool of spools) {
      if (taken.has(spool.spool_id) || ruledOut(spool)) continue
      if (slot?.material && spool.material.toUpperCase() !== slot.material.toUpperCase()) continue
      const have = rgb(spool.colour)
      if (!want || !have) continue
      const distance = Math.hypot(...want.map((value, index) => value - have[index]!))
      if (distance > COLOUR_MATCH_DISTANCE) continue
      // The server's order already puts loaded-here first, so ties keep it.
      if (best === null || distance < best.distance) best = { distance, spool }
    }
    if (!best) return []
    taken.add(best.spool.spool_id)
    return [{ slot_id: choice.slot_id, spool_id: best.spool.spool_id }]
  })
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
 * The picker's warnings for the plan the user is actually looking at.
 *
 * The server computes the same two rules for its own opening selection, but that
 * answer stops being true the moment a slot is changed — which is the entire point of
 * the picker. Both rules are a read of data the browser already holds, so they are
 * recomputed here rather than re-fetched or, worse, dropped.
 *
 * There are only two on purpose. Whether these filaments can share a plate, and which
 * AMS tray each one is drawn from, are Bambuddy's questions: its eligibility report
 * answers them beside these, and its scheduler resolves the tray at dispatch.
 */
export function checkPlan(
  options: FilamentOptions,
  plan: SlotChoice[],
  copies: number,
): FilamentWarning[] {
  const byId = new Map((options.spools ?? []).map((spool) => [spool.spool_id, spool]))
  const printerId = options.printer_id
  const found: FilamentWarning[] = []

  for (const slot of options.slots ?? []) {
    const choice = plan.find((entry) => entry.slot_id === slot.slot_id)
    const spool = choice ? byId.get(choice.spool_id) : undefined
    if (!spool) {
      found.push({
        kind: 'no-choice',
        slot_id: slot.slot_id,
        message: `Slot ${slot.slot_id} has no filament chosen.`,
      })
      continue
    }

    const label = spoolLabel(spool)
    if (!spool.loaded) {
      found.push({
        kind: 'not-loaded',
        slot_id: slot.slot_id,
        message: spool.storage_location
          ? `Load ${label} into the printer before this prints — it is stored in ${spool.storage_location}.`
          : `Load ${label} into the printer before this prints.`,
      })
    } else if (
      printerId !== null &&
      printerId !== undefined &&
      spool.loaded.printer_id !== printerId
    ) {
      found.push({
        kind: 'not-loaded',
        slot_id: slot.slot_id,
        message: `${label} is loaded in ${spool.loaded.printer_name ?? 'another printer'}, not in ${
          options.printer_name ?? 'the chosen printer'
        } — move it into an AMS slot there first.`,
      })
    }

    const needed = slotNeed(slot, copies)
    const left = spool.remaining_g
    if (needed !== null && left !== null && left !== undefined && left < needed) {
      found.push({
        kind: 'low-filament',
        slot_id: slot.slot_id,
        message: `${label} has about ${Math.round(left)} g left and this needs ${Math.round(
          needed,
        )} g.`,
      })
    }
  }

  return found
}

/**
 * The picker's opening selection (#78): the spools this model last printed with, slot by
 * slot, and the server's auto-match wherever nothing is remembered or the remembered
 * spool is no longer in the inventory.
 */
export function seedPlan(options: FilamentOptions, remembered: SlotChoice[]): SlotChoice[] {
  const inventory = new Set((options.spools ?? []).map((spool) => spool.spool_id))
  return (options.slots ?? []).flatMap((slot) => {
    const kept = remembered.find(
      (choice) => choice.slot_id === slot.slot_id && inventory.has(choice.spool_id),
    )
    const choice = kept ?? options.suggested?.find((entry) => entry.slot_id === slot.slot_id)
    return choice ? [{ ...choice }] : []
  })
}
