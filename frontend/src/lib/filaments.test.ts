import { describe, expect, it } from 'vitest'
import type { FilamentOptions, FilamentWarning, SlotNeed, SpoolOption } from '../api/types'
import {
  NO_FILTERS,
  choiceReasons,
  plateFamilies,
  colourName,
  tileGroups,
  trayCode,
  carriedPlan,
  checkPlan,
  facets,
  filterSpools,
  fromTrays,
  trayChoiceLabel,
  loadedLabel,
  seedPlan,
  slicedPlateSlots,
  slotNeed,
  withSlicedGrams,
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
  return { slot_id: 1, material: 'PLA', colour: '#0047BB', used_grams: 4.8, colour_matches: [], ...rest }
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
      'AMS-A · slot 2',
    )
  })

  it("names the unit the way Bambuddy does, by letter (the fourth AMS is not 'AMS 3')", () => {
    const at = (ams_id: number) => loadedLabel(spool({ loaded: loaded({ ams_id, tray_id: 3 }) }), 1)
    expect(at(3)).toBe('AMS-D · slot 4')
    expect(at(128)).toBe('HT-A · slot 4')
    expect(at(129)).toBe('HT-B · slot 4')
    expect(at(255)).toBe('External · slot 4')
  })

  it('names the other printer when the spool is not in the one being printed on', () => {
    const away = spool({ loaded: loaded({ printer_id: 2, printer_name: '3DP-77A-114' }) })
    expect(loadedLabel(away, 1)).toBe('AMS-A · slot 2 · on 3DP-77A-114')
    // Same printer: repeating its name on every row would say nothing.
    expect(loadedLabel(away, 2)).toBe('AMS-A · slot 2')
    // No printer chosen yet, so there is nothing to differ from.
    expect(loadedLabel(away, null)).toBe('AMS-A · slot 2')
  })

  it('falls back when Bambuddy did not name the other printer', () => {
    expect(loadedLabel(spool({ loaded: loaded({ printer_id: 2, printer_name: null }) }), 1)).toBe(
      'AMS-A · slot 2 · on another printer',
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

describe('withSlicedGrams (#2365)', () => {
  const options = { slots: [slot({ slot_id: 1, used_grams: null }), slot({ slot_id: 2, used_grams: 0 })] } as FilamentOptions

  it("takes each slot's grams from the slice", () => {
    const merged = withSlicedGrams(options, { slots: [{ slot_id: 1, grams: 13 }, { slot_id: 2, grams: 0.1 }] })
    expect(merged.slots?.map((each) => each.used_grams)).toEqual([13, 0.1])
  })

  it('leaves a slot the slice has no grams for, and everything without a slice', () => {
    const merged = withSlicedGrams(options, { slots: [{ slot_id: 1, grams: null }] })
    expect(merged.slots?.map((each) => each.used_grams)).toEqual([null, 0])
    expect(withSlicedGrams(options, null)).toBe(options)
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
      track_switch: false,
      source: 'inventory',
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

  it('warns when slots sharing a spool need more of it between them than it holds (#2851)', () => {
    const shared = options({
      slots: [
        { slot_id: 1, used_grams: 10 },
        { slot_id: 2, used_grams: 10 },
        { slot_id: 3, used_grams: 10 },
        { slot_id: 4, used_grams: 0 },
      ],
      spools: [spool({ spool_id: 1, remaining_g: 25, loaded: loaded({}) })],
    })
    const plan = [1, 2, 3, 4].map((slot_id) => ({ slot_id, spool_id: 1 }))
    const found = checkPlan(shared, plan, 1)
    expect(found.map((w) => w.slot_id)).toEqual([1, 2, 3])
    expect(found.every((w) => w.message.includes('slots 1, 2 and 3 need 30 g of it between them'))).toBe(true)
    // Two of them: 20 g of 25.
    expect(checkPlan(shared, plan.slice(0, 2), 1).filter((w) => w.kind === 'low-filament')).toEqual([])
  })

  it('says so when a slot has nothing chosen', () => {
    expect(checkPlan(options(), [], 1).map((w) => w.kind)).toEqual(['no-choice'])
  })
})

describe('fromTrays', () => {
  it('reads only an explicit trays source as trays; a missing one is the inventory', () => {
    expect(fromTrays({ source: 'trays' })).toBe(true)
    expect(fromTrays({ source: 'inventory' })).toBe(false)
    expect(fromTrays({})).toBe(false)
  })
})

describe('seedPlan', () => {
  const options: FilamentOptions = {
    library_file_id: 1,
    track_switch: false,
    source: 'inventory',
    slots: [
      slot({ slot_id: 1, colour_matches: [1, 2, 3] }),
      slot({ slot_id: 2, colour: '#FF1493', colour_matches: [1, 2, 3] }),
    ],
    spools: [spool({ spool_id: 1 }), spool({ spool_id: 2 }), spool({ spool_id: 3 })],
    suggested: [
      { slot_id: 1, spool_id: 1 },
      { slot_id: 2, spool_id: 2 },
    ],
    warnings: [],
  }

  it('opens on the spools this model last printed with', () => {
    expect(seedPlan(options, [{ slot_id: 2, spool_id: 3 }])).toEqual([
      { slot_id: 1, spool_id: 1 },
      { slot_id: 2, spool_id: 3 },
    ])
  })

  it('falls back to the auto-match for a remembered spool no longer in the inventory', () => {
    expect(seedPlan(options, [{ slot_id: 1, spool_id: 99 }])).toEqual(options.suggested)
  })

  it('falls back to the auto-match for a remembered spool whose colour no longer fits (#933)', () => {
    const recoloured: FilamentOptions = {
      ...options,
      slots: [options.slots![0]!, slot({ slot_id: 2, colour: '#FF1493', colour_matches: [2] })],
    }
    expect(seedPlan(recoloured, [{ slot_id: 2, spool_id: 3 }])).toEqual(options.suggested)
  })

  it('ignores a remembered slot this plate does not have', () => {
    expect(seedPlan(options, [{ slot_id: 3, spool_id: 3 }])).toEqual(options.suggested)
  })

  it('falls back to the auto-match for a remembered translucent spool on an opaque slot (#6262)', () => {
    const clear: FilamentOptions = {
      ...options,
      spools: [spool({ spool_id: 1 }), spool({ spool_id: 2 }), spool({ spool_id: 3, rgba: '#FF149380' })],
    }
    expect(seedPlan(clear, [{ slot_id: 2, spool_id: 3 }])).toEqual(options.suggested)
  })
})

describe('carriedPlan', () => {
  const options: FilamentOptions = {
    library_file_id: 1,
    track_switch: false,
    source: 'inventory',
    slots: [
      slot({ slot_id: 1, colour_matches: [1, 2, 3] }),
      slot({ slot_id: 2, colour: '#FF1493', colour_matches: [1, 2, 3] }),
    ],
    spools: [spool({ spool_id: 1 }), spool({ spool_id: 2 }), spool({ spool_id: 3 })],
    suggested: [
      { slot_id: 1, spool_id: 1 },
      { slot_id: 2, spool_id: 2 },
    ],
    warnings: [],
  }

  it("keeps the spool a re-arrange was made for, whatever its colour (#933 is for remembered ones)", () => {
    const recoloured: FilamentOptions = {
      ...options,
      slots: [options.slots![0]!, slot({ slot_id: 2, colour: '#FF1493', colour_matches: [2] })],
    }
    expect(carriedPlan(recoloured, [{ slot_id: 2, spool_id: 3 }])).toEqual([
      { slot_id: 1, spool_id: 1 },
      { slot_id: 2, spool_id: 3 },
    ])
  })

  it('drops a carried spool no longer in the inventory, and a slot the new file lacks', () => {
    expect(
      carriedPlan(options, [
        { slot_id: 1, spool_id: 99 },
        { slot_id: 3, spool_id: 3 },
      ]),
    ).toEqual(options.suggested)
  })
})

describe('slicedPlateSlots (#2702 review)', () => {
  const slots = [1, 2].map((slot_id) => ({ slot_id, colour_matches: [] }))

  it("is unknown for a two-plate file under All plates, or with another plate's slice shown", () => {
    expect(slicedPlateSlots(slots, { plates: 2, chosen: 'all', sliced: 1 })).toBeNull()
    expect(slicedPlateSlots(slots, { plates: 2, chosen: 1, sliced: 2 })).toBeNull()
  })

  it("is unknown while the slice shown is still another plate's, stale (#2702 review)", () => {
    // Plate 2 just chosen: its slots are read at once, but plate 1's slice stays shown.
    expect(slicedPlateSlots(slots, { plates: 2, chosen: 2, sliced: 1 })).toBeNull()
    expect(slicedPlateSlots(slots, { plates: 2, chosen: 2, sliced: null })).toBeNull()
  })

  it("is the dialog's slots for the chosen plate's own slice, or a one-plate file", () => {
    expect(slicedPlateSlots(slots, { plates: 2, chosen: 2, sliced: 2 })).toBe(slots)
    expect(slicedPlateSlots(slots, { plates: 1, chosen: 'all', sliced: 1 })).toBe(slots)
  })
})

describe('trayChoiceLabel', () => {
  it('says what is in a tray and where', () => {
    expect(
      trayChoiceLabel({ spool_id: -1, material: 'PLA', colour: '#0047BB', colour_word: 'blue', label: 'AMS-A slot 1' }),
    ).toBe('PLA blue in AMS-A slot 1')
    expect(
      trayChoiceLabel({ spool_id: -1, material: 'PETG', colour: null, loaded: { printer_id: 0, ams_id: 1, tray_id: 2 } }),
    ).toMatch(/^PETG in AMS-B/)
  })

  // #6885 — one name per tray: the spool's own colour name wins over the colour word.
  it("names the tray by the spool's own colour name when it holds one", () => {
    expect(
      trayChoiceLabel({
        spool_id: -3,
        material: 'PLA',
        colour: '#00B1B7',
        colour_word: 'teal',
        color_name: 'Turquoise',
        label: 'AMS-C slot 1',
      }),
    ).toBe('PLA Turquoise in AMS-C slot 1')
  })
})

describe('colourName (#6885)', () => {
  it('takes the spool’s own name, else the colour word, else the hex code', () => {
    expect(colourName(spool({ color_name: 'Navy Blue', colour_word: 'navy' }), true)).toBe('Navy Blue')
    expect(colourName(spool({ color_name: null, colour_word: 'navy' }), true)).toBe('navy')
    expect(colourName(spool({ color_name: null, colour_word: null, colour: '#000080' }), true)).toBe('#000080')
  })

  it("never names a Bambuddy-path tray by its place, \"what's in …\"", () => {
    const tray = spool({ tray_only: true, color_name: "what's in AMS-D slot 4", colour_word: 'black' })
    expect(colourName(tray, false)).toBe('black')
  })
})

describe('trayCode (#6885)', () => {
  it('codes a tray as Bambu Studio maps it', () => {
    expect(trayCode({ loaded: loaded({ ams_id: 0, tray_id: 2 }) })).toBe('A3')
    expect(trayCode({ loaded: loaded({ ams_id: 3, tray_id: 3 }) })).toBe('D4')
    expect(trayCode({ loaded: loaded({ ams_id: 128, tray_id: 0 }) })).toBe('HT-A')
    expect(trayCode({ loaded: loaded({ ams_id: 255, tray_id: 1 }), label: 'External R' })).toBe('Ext R')
    expect(trayCode({ loaded: null })).toBeNull()
  })
})

describe('choiceReasons (#6885)', () => {
  const grey: SlotNeed = { slot_id: 1, material: 'PLA', colour: '#A7AEB2', colour_matches: [-4] }

  it("says a close colour in another material is not the file's material", () => {
    const petg = spool({ spool_id: -4, material: 'PETG', color_name: 'Grey', colour: '#A7AEB2' })
    expect(choiceReasons(grey, petg, true)).toEqual(["PETG instead of the file's PLA"])
  })

  it("says a far colour is not the file's colour", () => {
    const teal = spool({ spool_id: -2, material: 'PLA Basic', color_name: null, colour_word: 'teal', colour: '#00B1B7' })
    expect(choiceReasons(grey, teal, true)).toEqual(["teal, not the file's colour"])
  })

  it("judges a slot with no material by the plate's family (#6885 review)", () => {
    const undeclared: SlotNeed = { slot_id: 2, colour: '#A7AEB2', colour_matches: [-4] }
    const petg = spool({ spool_id: -4, material: 'PETG', colour: '#A7AEB2' })
    expect(choiceReasons(undeclared, petg, true, ['PLA'])).toEqual(["PETG instead of the plate's PLA"])
    expect(choiceReasons(undeclared, petg, true, [])).toEqual([])
  })

  it('takes the plate’s family from the declared slots, else from most of the choices', () => {
    const spools = [spool({ spool_id: 1, material: 'PLA Basic' }), spool({ spool_id: 2, material: 'PLA Matte' }), spool({ spool_id: 3, material: 'PETG' })]
    const plan = [1, 2, 3].map((id) => ({ slot_id: id, spool_id: id }))
    const undeclared = [1, 2, 3].map((slot_id) => ({ slot_id, colour_matches: [] }))
    expect(plateFamilies({ slots: undeclared, spools }, plan)).toEqual(['PLA'])
    expect(plateFamilies({ slots: [...undeclared, { slot_id: 4, material: 'PETG HF' }], spools }, plan)).toEqual(['PETG'])
    expect(plateFamilies({ slots: undeclared, spools }, plan.slice(1))).toEqual([])
  })

  it('says nothing for a close match in the same family', () => {
    const match = spool({ spool_id: -4, material: 'PLA Matte', colour: '#A7AEB2' })
    expect(choiceReasons(grey, match, true)).toEqual([])
  })
})

describe('tileGroups (#6885)', () => {
  const tray = (spool_id: number, ams_id: number, tray_id: number, rest: Partial<SpoolOption> = {}) =>
    spool({ spool_id, loaded: loaded({ ams_id, tray_id }), ...rest })

  it('puts the close matches first, then the units in Bambu Studio’s order', () => {
    const spools = [
      tray(-17, 255, 0, { colour: '#FFFFFF' }),
      tray(-4, 128, 0, { material: 'PETG', colour: '#A7AEB2' }),
      tray(-1, 0, 0, { colour: '#C12E1F' }),
      tray(-5, 1, 0, { colour: '#A0A8AC' }),
      tray(-2, 0, 1, { colour: '#000080' }),
      spool({ spool_id: 9, colour: '#A7AEB2' }),
    ]
    const slot: SlotNeed = { slot_id: 1, material: 'PLA', colour: '#A7AEB2', colour_matches: [-4, -5, 9] }
    const groups = tileGroups(spools, slot, 1)
    expect(groups.map((group) => group.label)).toEqual([
      'Closest to the file',
      'AMS-A',
      'HT-A',
      'External',
    ])
    // The PETG is the colour, not the material: it stays under its unit.
    expect(groups[0]!.spools.map((entry) => entry.spool_id)).toEqual([-5, 9])
    expect(groups[1]!.spools.map((entry) => entry.spool_id)).toEqual([-1, -2])
  })

  it('has no close-match group for a slot with no colour', () => {
    const groups = tileGroups([tray(-1, 0, 0)], { slot_id: 1, colour_matches: [-1] }, 1)
    expect(groups.map((group) => group.label)).toEqual(['AMS-A'])
  })

  it("keeps one printer's units together when no printer is chosen (#6885 review)", () => {
    const at = (spool_id: number, printer_id: number, ams_id: number) =>
      spool({ spool_id, loaded: loaded({ printer_id, printer_name: `P${printer_id}`, ams_id, tray_id: 0 }) })
    const groups = tileGroups([at(1, 2, 0), at(2, 1, 1), at(3, 1, 0), at(4, 2, 1)], { slot_id: 1, colour_matches: [] }, null)
    expect(groups.map((group) => group.label)).toEqual(['P1 · AMS-A', 'P1 · AMS-B', 'P2 · AMS-A', 'P2 · AMS-B'])
    // By number: printer 2 before printer 10.
    const ten = tileGroups([at(5, 10, 0), at(6, 2, 0)], { slot_id: 1, colour_matches: [] }, null)
    expect(ten.map((group) => group.label)).toEqual(['P2 · AMS-A', 'P10 · AMS-A'])
  })

  it('puts a spool loaded in another printer apart', () => {
    const away = spool({ spool_id: 30, loaded: loaded({ printer_id: 2 }) })
    expect(tileGroups([away], { slot_id: 1, colour_matches: [] }, 1).map((group) => group.label)).toEqual([
      'In another printer',
    ])
  })
})
