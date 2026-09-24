import { describe, expect, it } from 'vitest'
import type { FilamentOptions, FilamentWarning, SlotNeed, SpoolOption } from '../api/types'
import {
  NO_FILTERS,
  checkPlan,
  facets,
  filterSpools,
  loadedLabel,
  slotNeed,
  spoolLabel,
  warningsFor,
} from './filaments'

function spool(rest: Partial<SpoolOption> = {}): SpoolOption {
  return {
    spool_id: 1,
    material: 'PLA',
    subtype: 'Basic',
    brand: 'Bambu Lab',
    color_name: 'Blue',
    colour: '#0047BB',
    slicer_filament: 'GFA00',
    slicer_filament_name: 'Bambu PLA Basic @BBL H2C 0.4 nozzle',
    remaining_g: 800,
    storage_location: null,
    loaded: null,
    ...rest,
  }
}

function loaded(rest: Partial<NonNullable<SpoolOption['loaded']>> = {}) {
  return {
    printer_id: 1,
    printer_name: '3DP-31B-598',
    ams_id: 0,
    tray_id: 1,
    global_tray_id: 1,
    extruder: 0,
    inlet: null,
    is_ams_ht: false,
    is_external: false,
    ...rest,
  }
}

function slot(rest: Partial<SlotNeed> = {}): SlotNeed {
  return { slot_id: 1, material: 'PLA', colour: '#0047BB', used_grams: 4.8, ...rest }
}

describe('spoolLabel', () => {
  it('joins the brand, material, subtype and colour name', () => {
    expect(spoolLabel(spool({ material: 'PETG', color_name: 'Misty Blue' }))).toBe(
      'Bambu Lab PETG Basic — Misty Blue',
    )
  })

  it('joins only what the spool actually carries', () => {
    // A spool added by hand has a material and nothing else.
    expect(spoolLabel(spool({ brand: null, subtype: null, color_name: null }))).toBe('PLA')
    expect(spoolLabel(spool({ brand: null, material: 'PLA', subtype: null }))).toBe('PLA — Blue')
  })
})

describe('loadedLabel', () => {
  it('is null for a spool on the shelf', () => {
    expect(loadedLabel(spool(), 1)).toBeNull()
  })

  it('counts AMS slots from 1, the way the printer does', () => {
    // tray_id is 0-based on the wire; Bambu's own UI numbers the slots 1-4.
    expect(loadedLabel(spool({ loaded: loaded({ ams_id: 0, tray_id: 1 }) }), 1)).toBe(
      'AMS 0 · slot 2',
    )
  })

  it('names the other printer when the spool is not in the one being printed on', () => {
    const away = spool({ loaded: loaded({ printer_id: 2, printer_name: '3DP-77A-114' }) })
    expect(loadedLabel(away, 1)).toBe('AMS 0 · slot 2 · on 3DP-77A-114')
    // Same printer: repeating its name on every row would say nothing.
    expect(loadedLabel(away, 2)).toBe('AMS 0 · slot 2')
    // No printer chosen yet, so there is nothing to differ from.
    expect(loadedLabel(away, null)).toBe('AMS 0 · slot 2')
  })

  it('falls back when Bambuddy did not name the other printer', () => {
    expect(loadedLabel(spool({ loaded: loaded({ printer_id: 2, printer_name: null }) }), 1)).toBe(
      'AMS 0 · slot 2 · on another printer',
    )
  })
})

describe('slotNeed', () => {
  it('multiplies by the copies', () => {
    expect(slotNeed(slot({ used_grams: 4.8 }), 3)).toBeCloseTo(14.4)
  })

  it('reads 0 and null alike as unknown', () => {
    // An unsliced plate answers `used_grams: 0` for every slot. Reading that as "needs
    // nothing" would declare every spool sufficient and print "0 g".
    expect(slotNeed(slot({ used_grams: 0 }), 2)).toBeNull()
    expect(slotNeed(slot({ used_grams: null }), 2)).toBeNull()
  })
})

describe('filterSpools', () => {
  const spools = [
    spool({ spool_id: 1, material: 'PLA', subtype: 'Silk', brand: 'Bambu Lab', color_name: 'Blue' }),
    spool({
      spool_id: 2,
      material: 'PETG',
      subtype: 'Basic',
      brand: 'Bambu Lab',
      color_name: 'Misty Blue',
      loaded: loaded(),
    }),
    spool({
      spool_id: 3,
      material: 'PLA',
      subtype: 'Basic',
      brand: 'Elegoo',
      color_name: 'Deep Pink',
      remaining_g: 2,
    }),
  ]
  const ids = (rows: SpoolOption[]) => rows.map((row) => row.spool_id)

  it('passes everything through with no filters set', () => {
    expect(ids(filterSpools(spools, NO_FILTERS, 4.8))).toEqual([1, 2, 3])
  })

  it('narrows by material, subtype and brand', () => {
    expect(ids(filterSpools(spools, { ...NO_FILTERS, material: 'PLA' }))).toEqual([1, 3])
    expect(ids(filterSpools(spools, { ...NO_FILTERS, subtype: 'Silk' }))).toEqual([1])
    expect(ids(filterSpools(spools, { ...NO_FILTERS, brand: 'Elegoo' }))).toEqual([3])
  })

  it('searches the colour name, brand, material, subtype and slicer preset name', () => {
    expect(ids(filterSpools(spools, { ...NO_FILTERS, search: 'misty' }))).toEqual([2])
    expect(ids(filterSpools(spools, { ...NO_FILTERS, search: 'elegoo' }))).toEqual([3])
    expect(ids(filterSpools(spools, { ...NO_FILTERS, search: 'PETG' }))).toEqual([2])
    expect(ids(filterSpools(spools, { ...NO_FILTERS, search: 'silk' }))).toEqual([1])
    expect(ids(filterSpools(spools, { ...NO_FILTERS, search: '0.4 nozzle' }))).toEqual([1, 2, 3])
    // Case-insensitive and trimmed, because the box is typed into.
    expect(ids(filterSpools(spools, { ...NO_FILTERS, search: '  BLUE ' }))).toEqual([1, 2])
  })

  it('keeps only loaded spools when asked', () => {
    expect(ids(filterSpools(spools, { ...NO_FILTERS, loadedOnly: true }))).toEqual([2])
  })

  it('hides a spool that has less than this print needs', () => {
    expect(ids(filterSpools(spools, { ...NO_FILTERS, enoughOnly: true }, 4.8))).toEqual([1, 2])
  })

  it('hides NOTHING when the grams are unknown', () => {
    // The plate has not been sliced, so there is no figure to compare against. Filtering
    // on it would empty the list for the commonest case and read as an inventory fault.
    expect(ids(filterSpools(spools, { ...NO_FILTERS, enoughOnly: true }, null))).toEqual([1, 2, 3])
  })

  it('keeps a spool whose own remaining weight is unknown', () => {
    // An untagged spool reports `remain: -1`, which the backend normalises to null.
    const untagged = [spool({ spool_id: 9, remaining_g: null })]
    expect(ids(filterSpools(untagged, { ...NO_FILTERS, enoughOnly: true }, 500))).toEqual([9])
  })

  it('combines filters', () => {
    expect(
      ids(filterSpools(spools, { ...NO_FILTERS, material: 'PLA', enoughOnly: true }, 4.8)),
    ).toEqual([1])
  })
})

describe('facets', () => {
  it('lists each distinct value once, sorted, skipping the nulls', () => {
    expect(
      facets([
        spool({ material: 'PETG', subtype: 'Magic', brand: 'Cookiecad' }),
        spool({ material: 'PLA', subtype: 'Basic', brand: 'Bambu Lab' }),
        spool({ material: 'PLA', subtype: null, brand: null }),
      ]),
    ).toEqual({
      materials: ['PETG', 'PLA'],
      subtypes: ['Basic', 'Magic'],
      brands: ['Bambu Lab', 'Cookiecad'],
    })
  })

  it('is empty for an empty inventory', () => {
    expect(facets([])).toEqual({ materials: [], subtypes: [], brands: [] })
  })
})

describe('warningsFor', () => {
  const warnings: FilamentWarning[] = [
    { kind: 'not-loaded', slot_id: 2, message: 'slot two' },
    { kind: 'low-filament', slot_id: 1, message: 'slot one' },
  ]

  it('picks out one slot’s warnings', () => {
    expect(warningsFor(warnings, 1).map((w) => w.message)).toEqual(['slot one'])
    expect(warningsFor(warnings, 3)).toEqual([])
  })
})

describe('checkPlan', () => {
  /**
   * The picker recomputes these because the server's own answer describes ITS opening
   * selection, and stops being true the moment a slot is changed — which is the whole
   * point of the picker.
   */
  function options(rest: Partial<FilamentOptions> = {}): FilamentOptions {
    return {
      library_file_id: 1,
      printer_id: 1,
      printer_name: 'Printer A',
      slots: [{ slot_id: 1, material: 'PLA', colour: '#0047BB', used_grams: 100 }],
      spools: [
        spool({ spool_id: 1, remaining_g: 800, loaded: loaded({}) }),
        spool({ spool_id: 2, remaining_g: 20, loaded: null, storage_location: 'Shelf A' }),
        spool({ spool_id: 3, remaining_g: 800, loaded: loaded({ printer_id: 2, printer_name: 'Printer B' }) }),
      ],
      suggested: [{ slot_id: 1, spool_id: 1 }],
      warnings: [],
      ...rest,
    }
  }

  it('says nothing about a loaded spool with enough left', () => {
    expect(checkPlan(options(), [{ slot_id: 1, spool_id: 1 }], 1)).toEqual([])
  })

  it('tracks the slot the user actually chose, not the server’s suggestion', () => {
    const found = checkPlan(options(), [{ slot_id: 1, spool_id: 2 }], 1)
    expect(found.map((w) => w.kind).sort()).toEqual(['low-filament', 'not-loaded'])
    expect(found.find((w) => w.kind === 'not-loaded')?.message).toContain('Shelf A')
  })

  it('names the other printer rather than calling the spool unloaded', () => {
    const found = checkPlan(options(), [{ slot_id: 1, spool_id: 3 }], 1)
    expect(found).toHaveLength(1)
    expect(found[0]?.message).toContain('Printer B')
  })

  it('multiplies the grams by the copies', () => {
    const plan = [{ slot_id: 1, spool_id: 1 }]
    expect(checkPlan(options(), plan, 1)).toEqual([])
    expect(checkPlan(options(), plan, 20).map((w) => w.kind)).toEqual(['low-filament'])
  })

  it('declines to judge when the plate has not been sliced', () => {
    // `used_grams: 0` is what an unsliced plate answers for every slot: unknown, never
    // "needs nothing".
    const unsliced = options({ slots: [{ slot_id: 1, used_grams: 0 }] })
    expect(checkPlan(unsliced, [{ slot_id: 1, spool_id: 1 }], 1000)).toEqual([])
  })

  it('says so when a slot has nothing chosen', () => {
    expect(checkPlan(options(), [], 1).map((w) => w.kind)).toEqual(['no-choice'])
  })
})
