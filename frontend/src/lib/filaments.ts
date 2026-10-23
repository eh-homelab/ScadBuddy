import type {
  FilamentOptions,
  FilamentWarning,
  SlicePreview,
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
 * Whether the choices are the printer's loaded trays (a native run, #3818) rather than
 * Bambuddy's inventory. Only an explicit `'trays'` counts: an older backend sends no
 * `source` at all during a rolling deploy, and that is the inventory (#4336 review M3).
 * Every reader of `source` goes through this, never `source !== 'inventory'`.
 */
export function fromTrays(options: Pick<FilamentOptions, 'source'> | { source?: string }): boolean {
  return options.source === 'trays'
}

/**
 * #3818 — a loaded tray as a native run's choice: `"PLA blue in AMS-A slot 1"`, from the
 * tray's material, its `colour_word` and its `label`, each the backend's (`trays.py`
 * `colour_word`, `tray_label`): nothing about the colour is worked out here.
 */
export function trayChoiceLabel(spool: SpoolOption): string {
  const where = spool.label ?? (spool.loaded ? trayPlace(spool.loaded) : null)
  const what = [spool.material, spool.colour_word].filter(Boolean).join(' ')
  return where ? `${what} in ${where}` : what
}

/**
 * A spool provider as a person reads it: Bambuddy's inventory, or another's name. A
 * provider's own label (`SpoolProvider.label`) is not on the wire, so any other key is
 * title-cased, its underscores read as spaces (`open_spool` is "Open Spool").
 */
export function providerLabel(provider: string): string {
  if (provider === 'bambuddy') return 'Bambuddy'
  if (provider === 'native') return 'ScadBuddy'
  return provider
    .split('_')
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

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
 * An AMS unit's name as Bambuddy shows it: by letter, so `ams_id` 3 is "AMS-D", the
 * fourth AMS, and 128 is "HT-A". 255 is the external spool and 6 the AMS Lite.
 */
export function amsUnitLabel(amsId: number): string {
  if (amsId === 255) return 'External'
  if (amsId === 6) return 'AMS Lite'
  const letter = String.fromCharCode(65 + (amsId >= 128 ? amsId - 128 : amsId))
  return amsId >= 128 ? `HT-${letter}` : `AMS-${letter}`
}

/**
 * An AMS tray as Bambuddy names it, "AMS-A · slot 4", with the printer first when
 * asked ("3DP-31B-598 · AMS-A · slot 4"): on a farm two printers each have an AMS-A
 * slot 1. The one wording for a loaded spool, in the print dialog and Settings alike.
 */
export function trayPlace(
  loaded: { printer_id: number; printer_name?: string | null; ams_id: number; tray_id: number },
  { printer = false }: { printer?: boolean } = {},
): string {
  const place = `${amsUnitLabel(loaded.ams_id)} · slot ${loaded.tray_id + 1}`
  return printer ? `${loaded.printer_name ?? `Printer ${loaded.printer_id}`} · ${place}` : place
}

/**
 * Where a spool physically is, or `null` when it is on the shelf.
 *
 * `tray_id` is the printer's 0-based index while Bambu's own UI counts AMS slots from
 * 1, so the human number wins, and the unit is named as Bambuddy names it. This is a
 * label and nothing more: which tray the print actually draws from is Bambuddy's to
 * decide at dispatch.
 *
 * `printerId` is the printer the print is scoped to. A spool loaded somewhere else is
 * still a legitimate choice, and naming the other printer is the difference between
 * "load this" and a silent surprise — but repeating the chosen printer on every row
 * would say nothing, so it is named only when it differs.
 */
export function loadedLabel(spool: SpoolOption, printerId?: number | null): string | null {
  const loaded = spool.loaded
  if (!loaded) return null
  let where = trayPlace(loaded)
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

/**
 * #2365 — the slots with the background slice's grams where it has them. A ScadBuddy
 * upload carries none until it is sliced, so without this every slot read "grams
 * unknown" beside the slice's own grams, and nothing could judge a spool short. `null`
 * (no slice for the plate the slots describe) leaves the options as they are.
 */
export function withSlicedGrams(
  options: FilamentOptions,
  slice: Pick<SlicePreview, 'slots'> | null,
): FilamentOptions {
  const sliced = new Map((slice?.slots ?? []).map((entry) => [entry.slot_id, entry.grams]))
  if (sliced.size === 0) return options
  return {
    ...options,
    slots: (options.slots ?? []).map((slot) => {
      const grams = sliced.get(slot.slot_id)
      // A slot the slice gives no grams keeps the 3MF's own figure: the intended fallback.
      return grams != null && grams > 0 ? { ...slot, used_grams: grams } : slot
    }),
  }
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
  // What every slot on one spool needs together, as the server's check (#2851): a spool
  // two slots share has to cover both, though each fits on its own.
  const sharing = new Map<number, { slot_id: number; needed: number }[]>()
  for (const slot of options.slots ?? []) {
    const choice = plan.find((entry) => entry.slot_id === slot.slot_id)
    const needed = slotNeed(slot, copies)
    if (choice && needed !== null) {
      sharing.set(choice.spool_id, [...(sharing.get(choice.spool_id) ?? []), { slot_id: slot.slot_id, needed }])
    }
  }

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
    } else if (needed !== null && left !== null && left !== undefined) {
      const on = sharing.get(spool.spool_id) ?? []
      const together = on.reduce((sum, other) => sum + other.needed, 0)
      if (on.length > 1 && left < together) {
        const ids = on.map((other) => other.slot_id).sort((a, b) => a - b)
        const names = `${ids.slice(0, -1).join(', ')} and ${ids.at(-1)}`
        found.push({
          kind: 'low-filament',
          slot_id: slot.slot_id,
          message: `${label} has about ${Math.round(left)} g left and slots ${names} need ${Math.round(
            together,
          )} g of it between them.`,
        })
      }
    }
  }

  return found
}

/**
 * The picker's opening selection (#78): the spools this model last printed with, slot by
 * slot, and the server's auto-match wherever nothing is remembered, the remembered spool
 * is no longer in the inventory, or its colour no longer fits the slot's (#933). Which
 * spools still fit is the server's call (`colour_matches`, the auto-match's own cut-off),
 * as the download's seed is (`seed_plan` in `bambuddy/filaments.py`).
 */
export function seedPlan(options: FilamentOptions, remembered: SlotChoice[]): SlotChoice[] {
  return (options.slots ?? []).flatMap((slot) => {
    const fits = new Set(slot.colour_matches ?? [])
    const kept = remembered.find(
      (choice) => choice.slot_id === slot.slot_id && fits.has(choice.spool_id),
    )
    const choice = kept ?? options.suggested?.find((entry) => entry.slot_id === slot.slot_id)
    return choice ? [{ ...choice }] : []
  })
}

/**
 * The selection after a re-arrange: the plan the user chose and arranged for, kept slot
 * by slot whatever its colours (a spool picked in this dialog is a choice, not a
 * remembered one that may have gone stale, #933), less any slot the new file lacks or
 * spool no longer in the inventory; the auto-match fills the rest.
 */
export function carriedPlan(options: FilamentOptions, carried: SlotChoice[]): SlotChoice[] {
  const inventory = new Set((options.spools ?? []).map((spool) => spool.spool_id))
  return (options.slots ?? []).flatMap((slot) => {
    const kept = carried.find(
      (choice) => choice.slot_id === slot.slot_id && inventory.has(choice.spool_id),
    )
    const choice = kept ?? options.suggested?.find((entry) => entry.slot_id === slot.slot_id)
    return choice ? [{ ...choice }] : []
  })
}

/**
 * #2702 review — the background slice's plate's own slots, or `null` when the dialog
 * does not hold them. The slice is always of one plate, while the dialog's slots are the
 * chosen plate's, or every plate's under "All plates". So they are the sliced plate's
 * only when that plate is the one chosen, or the file has one. `sliced` is the plate
 * the shown slice was made for (`usePreviewSlice`'s `plateId`), not the one on screen:
 * a stale slice stays shown while the next is sliced, and can be another plate's.
 */
export function slicedPlateSlots(
  slots: readonly SlotNeed[],
  { plates, chosen, sliced }: { plates: number; chosen: number | 'all'; sliced: number | null },
): readonly SlotNeed[] | null {
  if (plates <= 1) return slots
  return chosen === sliced ? slots : null
}
