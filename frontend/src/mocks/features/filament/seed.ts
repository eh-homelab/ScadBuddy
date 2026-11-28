import type { components } from '../../../api/schema'
import type { FilamentSpool } from '../../../api/types'
import { ORDER_CLOSED } from '../orders/state'

type S = components['schemas']

/**
 * #6262 — the Filament section's seed (`index.ts` serves it): the catalogs, spool types,
 * places, tags and ledger of a small shop, and 12 spools that cover the cases the section
 * draws apart: opaque, translucent (alpha `80`), clear (`10` and `00`), one that still
 * needs a type, one whose material is `Unknown` (Bambuddy's word when it had none), one
 * low on stock, one with no label weight, and one archived spool (#19) whose last tag is
 * on spool #3 now. #3, #8, #11 and #19 are Task 9's inventory spools, unchanged in what
 * the orders and settings mocks read (colour, grams, cost, tray). The ids are none of
 * Bambuddy's, so a reader that still takes a Bambuddy id shows up.
 *
 * Every `.ts` under `features/` is loaded as a mock feature and must export `handlers`
 * (`mocks/features.ts`): this one answers no route of its own.
 */
export const handlers = []

export const AT = '2026-11-15T07:00:00Z'

/** A seed id: its kind's prefix and its number. */
const id = (prefix: string, n: number) => `${prefix}-0000-4000-8000-${String(n).padStart(12, '0')}`
export const spoolId = (number: number) => id('62620000', number)
const typeId = (n: number) => id('62620001', n)
const catalogId = (n: number) => id('62620002', n)
const sensorId = (n: number) => id('62680001', n)
const locationId = (n: number) => id('62620003', n)
const tagId = (n: number) => id('62620004', n)

/** The registry printer the AMS trays belong to (Task 9's). */
export const H2C = { printer_id: '00000000-0000-4000-8000-00000000f262', printer_name: '3DP-31B-598', bambuddy_printer_id: null }

type Look = {
  id: string
  number: number
  material: string
  subtype?: string | null
  brand: string
  colour_name: string
  rgba: string
  grams_left: number
  cost_per_kg: string | null
  archived?: boolean
}

/**
 * A full SB spool from its look, with no type; its own and effective values agree. For
 * a test that builds its own `GET /filament/inventory` rows (Task 9).
 */
export function sbSpool(look: Look): FilamentSpool {
  return {
    id: look.id,
    number: look.number,
    type_id: null,
    brand_id: null,
    material_id: null,
    subtype_id: null,
    category_id: null,
    effect_id: null,
    colour_name: look.colour_name,
    rgba: look.rgba,
    extra_colours: null,
    diameter_mm: '1.75',
    density_g_cm3: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    bed_temp_min: null,
    bed_temp_max: null,
    label_weight_g: 1000,
    insert_type_id: null,
    low_stock_pct: null,
    cost_per_kg: look.cost_per_kg,
    location_id: null,
    weight_locked: false,
    note: null,
    grams_left: look.grams_left,
    last_weighed_g: null,
    last_weighed_at: null,
    last_drift_g: null,
    last_dried_at: null,
    last_dried_temp_c: null,
    last_dried_hours: null,
    hours_outside_dry: 0,
    needs_drying: false,
    last_used_at: null,
    archived_at: look.archived ? AT : null,
    created_at: AT,
    updated_at: AT,
    low_stock: false,
    drift_notice: false,
    overridden: [],
    names: { material: look.material, subtype: look.subtype ?? null, brand: look.brand },
    effective: { colour_name: look.colour_name, rgba: look.rgba, cost_per_kg: look.cost_per_kg, label_weight_g: 1000 },
  }
}

// -- catalogs -------------------------------------------------------------------------

const row = (n: number, name: string, sort_order = 0) => ({
  id: catalogId(n),
  name,
  sort_order,
  archived_at: null,
  created_at: AT,
  updated_at: AT,
})

/** A row of a catalog with aliases (#6706). */
const aliased = (n: number, name: string, sort_order = 0, aliases: string[] = []) => ({ ...row(n, name, sort_order), aliases })

function material(n: number, name: string, values: Partial<S['Material']> = {}): S['Material'] {
  return {
    ...aliased(n, name),
    family: null,
    density_g_cm3: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    bed_temp_min: null,
    bed_temp_max: null,
    drying_temp_c: null,
    drying_hours: null,
    max_hours_outside_dry: null,
    needs_enclosure: false,
    abrasive: false,
    hygroscopic: false,
    flexible: false,
    hardened_nozzle: false,
    note: null,
    ...values,
  }
}

function subtype(n: number, name: string, materialId: string): S['Subtype'] {
  return {
    ...aliased(n, name),
    material_id: materialId,
    density_g_cm3: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    bed_temp_min: null,
    bed_temp_max: null,
    drying_temp_c: null,
    drying_hours: null,
  }
}

export const M = { PLA: catalogId(1), PETG: catalogId(2), TPU: catalogId(3), UNKNOWN: catalogId(4) }
export const ST = { BASIC: catalogId(10), MATTE: catalogId(11), SILK: catalogId(12), HF: catalogId(13), A95: catalogId(14) }
export const B = {
  BAMBU: catalogId(20),
  INSIGNIA: catalogId(21),
  INLAND: catalogId(22),
  POLYMAKER: catalogId(23),
  ELEGOO: catalogId(24),
  SUNLU: catalogId(25),
}
export const E = { TRANSLUCENT: catalogId(40), CLEAR: catalogId(41), SILK: catalogId(42) }
export const IT = { BAMBU_REUSABLE: catalogId(60), CARDBOARD: catalogId(61) }
export const LK = { ROOM: catalogId(80), SHELF: catalogId(81), DRY_BOX: catalogId(82), AMS: catalogId(83), PRINTER: catalogId(84) }

export type Catalogs = {
  materials: S['Material'][]
  subtypes: S['Subtype'][]
  brands: S['Brand'][]
  categories: S['Category'][]
  effects: S['Effect'][]
  'insert-materials': S['InsertMaterial'][]
  'insert-types': S['InsertType'][]
  colours: S['Colour'][]
  'location-kinds': S['LocationKind'][]
}

function catalogs(): Catalogs {
  return {
    materials: [
      material(1, 'PLA', { family: 'PLA', density_g_cm3: '1.24', nozzle_temp_min: 190, nozzle_temp_max: 230, bed_temp_min: 35, bed_temp_max: 65, drying_temp_c: 55, drying_hours: '8' }),
      material(2, 'PETG', { family: 'PETG', density_g_cm3: '1.27', nozzle_temp_min: 230, nozzle_temp_max: 260, bed_temp_min: 65, bed_temp_max: 80, drying_temp_c: 65, drying_hours: '8', hygroscopic: true, max_hours_outside_dry: '48' }),
      material(3, 'TPU', { family: 'TPU', density_g_cm3: '1.21', nozzle_temp_min: 220, nozzle_temp_max: 240, flexible: true }),
      material(4, 'Unknown', { note: 'What a Bambuddy spool with no material is pulled as.' }),
    ],
    subtypes: [
      subtype(10, 'Basic', M.PLA),
      subtype(11, 'Matte', M.PLA),
      subtype(12, 'Silk', M.PLA),
      subtype(13, 'HF', M.PETG),
      subtype(14, '95A', M.TPU),
    ],
    brands: [
      aliased(20, 'Bambu Lab'),
      aliased(21, 'Insignia'),
      aliased(22, 'Inland'),
      aliased(23, 'Polymaker'),
      aliased(24, 'Elegoo'),
      aliased(25, 'Sunlu'),
    ],
    categories: [aliased(30, 'Standard'), aliased(31, 'Engineering', 1)],
    effects: [aliased(40, 'Translucent', 0, ['Transparent']), aliased(41, 'Clear', 1), aliased(42, 'Silk', 2)],
    'insert-materials': [row(50, 'Plastic'), row(51, 'Cardboard', 1)],
    'insert-types': [
      { ...row(60, 'Bambu reusable spool'), empty_weight_g: 250, insert_material_id: catalogId(50), note: null, bb_catalog_id: null, empty_weight_basis: 'catalog', placeholder: false, derived_count: 0 },
      { ...row(61, 'Cardboard spool', 1), empty_weight_g: 180, insert_material_id: catalogId(51), note: null, bb_catalog_id: null, empty_weight_basis: 'catalog', placeholder: false, derived_count: 0 },
    ],
    colours: [
      // #6907: a synced row (`source`, `synced_at` its `updated_at`), and a person's own.
      { ...row(70, 'Jade White'), manufacturer: 'Bambu Lab', rgba: '#FFFFFFFF', material_id: M.PLA, material_name: 'PLA Basic', extra_colours: null, effect_id: null, source: 'bambu', synced_at: AT, bb_colour_id: null },
      { ...row(71, 'Hot Pink'), manufacturer: 'Bambu Lab', rgba: '#F5547CFF', material_id: M.PLA, material_name: null, extra_colours: null, effect_id: null, source: null, synced_at: null, bb_colour_id: null },
      { ...row(72, 'Translucent Orange'), manufacturer: 'Bambu Lab', rgba: '#FF6A1380', material_id: M.PETG, material_name: null, extra_colours: null, effect_id: E.TRANSLUCENT, source: null, synced_at: null, bb_colour_id: null },
      // #6912: a multi-colour row, whose pick fills the extra colours, and an archived one no pick offers.
      { ...row(73, 'Galaxy Duo'), manufacturer: 'Elegoo', rgba: '#1B3A8CFF', material_id: M.PLA, material_name: 'PLA Silk Dual', extra_colours: ['#2E8B57FF'], effect_id: E.SILK, source: 'filamentcolors', synced_at: AT, bb_colour_id: null },
      { ...row(74, 'Retired Grey'), archived_at: AT, manufacturer: 'Sunlu', rgba: '#808080FF', material_id: M.PLA, material_name: null, extra_colours: null, effect_id: null, source: null, synced_at: null, bb_colour_id: null },
    ],
    'location-kinds': [
      { ...row(80, 'Room'), dry_storage: false, ams: false, printer: false },
      { ...row(81, 'Shelf', 1), dry_storage: false, ams: false, printer: false },
      { ...row(82, 'Dry box', 2), dry_storage: true, ams: false, printer: false },
      { ...row(83, 'AMS tray', 3), dry_storage: false, ams: true, printer: false },
      { ...row(84, 'Printer', 4), dry_storage: false, ams: false, printer: true },
    ],
  }
}

// -- places ---------------------------------------------------------------------------

export const L = { WORKSHOP: locationId(1), SHELF_A: locationId(2), DRY_BOX: locationId(3), AMS_A1: locationId(4), AMS_A4: locationId(5), AMS_A: locationId(6), H2C: locationId(7) }

function place(n: number, name: string, kind_id: string, values: Partial<S['Location']> = {}): S['Location'] {
  return {
    ...row(n, name),
    id: locationId(n),
    kind_id,
    number: n,
    printer_root: false,
    station: false,
    parent_id: null,
    identifier: null,
    capacity: null,
    printer_id: null,
    ams_id: null,
    tray_id: null,
    note: null,
    ...values,
  }
}

function locations(): S['Location'][] {
  return [
    place(1, 'Workshop', LK.ROOM),
    place(2, 'Shelf A', LK.SHELF, { parent_id: L.WORKSHOP, capacity: 12 }),
    place(3, 'Dry box 1', LK.DRY_BOX, { parent_id: L.WORKSHOP, capacity: 4, sort_order: 1 }),
    place(4, 'H2C AMS-A slot 1', LK.AMS, { parent_id: L.AMS_A, printer_id: H2C.printer_id, ams_id: 0, tray_id: 0, sort_order: 2 }),
    place(6, 'H2C AMS-A', LK.AMS, { parent_id: L.H2C, printer_id: H2C.printer_id, ams_id: 0, sort_order: 4 }),
    place(7, '3DP-31B-598', LK.PRINTER, { printer_id: H2C.printer_id, printer_root: true, sort_order: 5 }),
    place(5, 'H2C AMS-A slot 4', LK.AMS, { parent_id: L.AMS_A, printer_id: H2C.printer_id, ams_id: 0, tray_id: 3, sort_order: 3 }),
  ]
}

// -- spool types ----------------------------------------------------------------------

type TypeLook = {
  n: number
  code: string
  brand: string
  material: string
  subtype?: string | null
  effect?: string | null
  colour_name: string
  rgba: string
  label_weight_g?: number
  cost_per_kg?: string | null
  insert?: string | null
  lead_time_days?: number | null
  safety_margin_days?: number | null
  drying_temp_c?: number | null
  drying_hours?: string | null
}

function spoolType(look: TypeLook): S['SpoolType'] {
  return {
    id: typeId(look.n),
    number: look.n,
    code: look.code,
    brand_id: look.brand,
    material_id: look.material,
    subtype_id: look.subtype ?? null,
    category_id: catalogId(30),
    effect_id: look.effect ?? null,
    colour_name: look.colour_name,
    rgba: look.rgba,
    extra_colours: null,
    diameter_mm: '1.75',
    density_g_cm3: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    bed_temp_min: null,
    bed_temp_max: null,
    drying_temp_c: look.drying_temp_c ?? null,
    drying_hours: look.drying_hours ?? null,
    label_weight_g: look.label_weight_g ?? 1000,
    insert_type_id: look.insert ?? IT.BAMBU_REUSABLE,
    low_stock_pct: null,
    cost_per_kg: look.cost_per_kg ?? null,
    lead_time_days: look.lead_time_days ?? null,
    safety_margin_days: look.safety_margin_days ?? null,
    filament_id: null,
    tdfp_id: null,
    note: null,
    archived_at: null,
    created_at: AT,
    updated_at: AT,
  }
}

export const T = {
  JADE: typeId(1),
  ORANGE: typeId(2),
  PINK: typeId(3),
  BLACK: typeId(4),
  POLY_CLEAR: typeId(5),
  INSIGNIA_CLEAR: typeId(8),
  GOLD: typeId(9),
  TPU_RED: typeId(10),
  MISTLETOE: typeId(11),
  INLAND_BLACK: typeId(19),
}

function types(): S['SpoolType'][] {
  return [
    spoolType({ n: 1, code: 'BL-PLA-BASIC-JADE-WHITE', brand: B.BAMBU, material: M.PLA, subtype: ST.BASIC, colour_name: 'Jade White', rgba: '#FFFFFFFF', cost_per_kg: '15.91' }),
    // #6269 — its own lead time and safety margin: at #2's burn on 2026-11-18 it runs out on 2027-03-15, so its reorder day (120 days before) is past and it says "Reorder now".
    spoolType({ n: 2, code: 'BL-PETG-HF-TRANSLUCENT-ORANGE', brand: B.BAMBU, material: M.PETG, subtype: ST.HF, effect: E.TRANSLUCENT, colour_name: 'Translucent Orange', rgba: '#FF6A1380', cost_per_kg: '19.99', lead_time_days: 30, safety_margin_days: 90, drying_temp_c: 70 }),
    spoolType({ n: 3, code: 'BL-PLA-BASIC-HOT-PINK', brand: B.BAMBU, material: M.PLA, subtype: ST.BASIC, colour_name: 'Hot Pink', rgba: '#F5547CFF', cost_per_kg: '15.91' }),
    spoolType({ n: 4, code: 'BL-PLA-BASIC-BLACK', brand: B.BAMBU, material: M.PLA, subtype: ST.BASIC, colour_name: 'Black', rgba: '#000000FF', cost_per_kg: '15.91' }),
    spoolType({ n: 5, code: 'PM-PETG-CLEAR', brand: B.POLYMAKER, material: M.PETG, effect: E.CLEAR, colour_name: 'Clear', rgba: '#FFFFFF10', cost_per_kg: '21.99', insert: IT.CARDBOARD }),
    spoolType({ n: 8, code: 'INS-PETG-CLEAR', brand: B.INSIGNIA, material: M.PETG, effect: E.CLEAR, colour_name: 'Clear', rgba: '#00000000', cost_per_kg: '15.96', insert: IT.CARDBOARD }),
    spoolType({ n: 9, code: 'EL-PLA-SILK-GOLD', brand: B.ELEGOO, material: M.PLA, subtype: ST.SILK, effect: E.SILK, colour_name: 'Gold', rgba: '#D4AF37FF', cost_per_kg: '17.99', insert: IT.CARDBOARD }),
    spoolType({ n: 10, code: 'SU-TPU-95A-RED', brand: B.SUNLU, material: M.TPU, subtype: ST.A95, colour_name: 'Red', rgba: '#C0392BFF', label_weight_g: 500, cost_per_kg: '29.98', insert: IT.CARDBOARD }),
    spoolType({ n: 11, code: 'BL-PLA-BASIC-MISTLETOE', brand: B.BAMBU, material: M.PLA, subtype: ST.BASIC, colour_name: 'Mistletoe Green', rgba: '#3F8E43FF' }),
    spoolType({ n: 19, code: 'IN-PLA-BLACK', brand: B.INLAND, material: M.PLA, colour_name: 'Black', rgba: '#1A1A1AFF', cost_per_kg: '14', insert: IT.CARDBOARD }),
  ]
}

const typePresets = (): S['TypePreset'][] => [
  { type_id: T.JADE, printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'GFA00', slicer_filament_name: 'Bambu PLA Basic @BBL H2C' },
  { type_id: T.PINK, printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'GFA00', slicer_filament_name: 'Bambu PLA Basic @BBL H2C' },
  { type_id: T.ORANGE, printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'GFG02', slicer_filament_name: 'Bambu PETG HF @BBL H2C' },
]

const typeKValues = (): S['TypeKValue'][] => [
  { type_id: T.JADE, printer_id: H2C.printer_id, extruder: 0, nozzle_diameter: '0.4', nozzle_type: 'hardened_steel', k_value: '0.020', name: 'Jade White 0.4', cali_idx: 3, setting_id: null, source: 'person' },
]

// -- spools ---------------------------------------------------------------------------

/** A spool's own columns, as `filament_spools` holds them: an override, or `null`. */
export type SpoolRow = Omit<FilamentSpool, 'effective' | 'overridden' | 'names' | 'low_stock' | 'drift_notice'>

function spool(number: number, values: Partial<SpoolRow>): SpoolRow {
  return {
    id: spoolId(number),
    number,
    type_id: null,
    brand_id: null,
    material_id: null,
    subtype_id: null,
    category_id: null,
    effect_id: null,
    colour_name: null,
    rgba: null,
    extra_colours: null,
    diameter_mm: null,
    density_g_cm3: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    bed_temp_min: null,
    bed_temp_max: null,
    label_weight_g: null,
    insert_type_id: null,
    low_stock_pct: null,
    cost_per_kg: null,
    location_id: null,
    weight_locked: false,
    note: null,
    data_origin: null,
    added_full: null,
    tag_encoded_at: null,
    grams_left: null,
    last_weighed_g: null,
    last_weighed_at: null,
    last_drift_g: null,
    last_dried_at: null,
    last_dried_temp_c: null,
    last_dried_hours: null,
    hours_outside_dry: 0,
    needs_drying: false,
    last_used_at: null,
    archived_at: null,
    created_at: AT,
    updated_at: AT,
    ...values,
  }
}

function spools(): SpoolRow[] {
  return [
    // #7682: its newest weighing has a breakdown (`events()`), so its drift is on the row.
    spool(1, { type_id: T.JADE, grams_left: 640, location_id: L.SHELF_A, last_weighed_g: 640, last_weighed_at: '2026-11-14T18:00:00Z', last_drift_g: -12 }),
    spool(2, { type_id: T.ORANGE, created_at: '2026-11-01T09:00:00Z', grams_left: 820, location_id: L.DRY_BOX, last_weighed_g: 820, last_weighed_at: '2026-11-13T09:00:00Z', last_used_at: '2026-11-09T11:00:00Z', hours_outside_dry: 61.5, last_dried_at: '2026-11-12T09:00:00Z', last_dried_temp_c: 65, last_dried_hours: '8' }),
    spool(3, { type_id: T.PINK, grams_left: 930, location_id: L.SHELF_A }),
    // Bambuddy added it when its AMS read the tag (#7149: `data_origin`).
    spool(4, { type_id: T.BLACK, grams_left: 120, location_id: L.AMS_A1, last_used_at: '2026-11-14T21:30:00Z', data_origin: 'rfid_auto', added_full: true }),
    spool(5, { type_id: T.POLY_CLEAR, grams_left: 700, location_id: L.DRY_BOX }),
    // Added by hand with only its material: it needs a type.
    spool(6, { material_id: M.PLA, colour_name: 'Teal', rgba: '#1B9AAAFF', label_weight_g: 1000, grams_left: 500 }),
    // Pulled from Bambuddy with no material, and no label weight: no grams arc.
    spool(7, { material_id: M.UNKNOWN, rgba: '#808080FF', grams_left: 300, note: 'Pulled from Bambuddy spool 41.' }),
    // Its tag is an OpenTag3D tag, written at a known time (#7149).
    spool(8, { type_id: T.INSIGNIA_CLEAR, grams_left: 400, location_id: L.AMS_A4, data_origin: 'opentag3d', tag_encoded_at: '2026-11-10T15:30:00Z' }),
    spool(9, { type_id: T.GOLD, grams_left: 1000 }),
    spool(10, { type_id: T.TPU_RED, grams_left: 250, low_stock_pct: 30 }),
    spool(11, { type_id: T.MISTLETOE, grams_left: 1000 }),
    spool(19, { type_id: T.INLAND_BLACK, grams_left: 0, archived_at: AT }),
  ]
}

// -- tags and the ledger --------------------------------------------------------------------

export const TAG = { MOVED: tagId(1), BLACK: tagId(2), LOOSE: tagId(3) }

function tags(): S['Tag'][] {
  const tag = (n: number, kind: S['Tag']['kind'], tag_uid: string, tray_uuid: string | null): S['Tag'] => ({
    id: tagId(n),
    kind,
    tag_uid,
    tray_uuid,
    raw_blob: null,
    note: null,
    created_at: '2026-11-01T10:00:00Z',
  })
  return [
    tag(1, 'ntag_openspool', '04A1B2C3D4E5F6', null),
    tag(2, 'bambu_mifare', 'A1B2C3D4', '7F3E1C0A9B8D4E2F8A6B5C4D3E2F1A0B'),
    tag(3, 'other', '04FFEE0011223344', null),
  ]
}

function bindings(): S['TagBinding'][] {
  const binding = (n: number, tag_id: string, spool_id: string, bound_at: string, values: Partial<S['TagBinding']> = {}): S['TagBinding'] => ({
    id: id('62620006', n),
    tag_id,
    spool_id,
    override: false,
    bound_at,
    unbound_at: null,
    reason: null,
    ...values,
  })
  return [
    // #19's tag, taken off when it ran out, is on #3 now.
    binding(1, TAG.MOVED, spoolId(19), '2026-11-01T10:00:00Z', { unbound_at: '2026-11-10T12:00:00Z', reason: 'spool empty' }),
    binding(2, TAG.MOVED, spoolId(3), '2026-11-10T12:05:00Z', { override: true, reason: 'reused on a new spool' }),
    binding(3, TAG.BLACK, spoolId(4), '2026-11-02T09:00:00Z'),
  ]
}

function events(): S['SpoolEvent'][] {
  let n = 0
  const event = (spool: number, at: string, kind: S['SpoolEvent']['kind'], values: Partial<S['SpoolEvent']> = {}): S['SpoolEvent'] => ({
    id: id('62620005', ++n),
    seq: n,
    spool_id: spoolId(spool),
    at,
    kind,
    source: null,
    printer_id: null,
    ams_id: null,
    tray_id: null,
    grams_before: null,
    grams_after: null,
    delta_g: null,
    ref_kind: null,
    ref_id: null,
    actor: 'person@example.com',
    data: {},
    ...values,
  })
  const printer = { printer_id: H2C.printer_id, ams_id: 0, tray_id: 0 }
  return [
    // #4 came from a Bambuddy pull (#7682: its anchor is Bambuddy's grams, not a reading).
    ...spools().map((row) => event(row.number, '2026-11-01T09:00:00Z', 'created', row.number === 4 ? { source: 'bb', grams_after: 268, ref_kind: 'bb_spool', ref_id: 'bb-4', actor: 'bambuddy' } : {})),
    event(19, '2026-11-01T10:00:00Z', 'tag_bound', { data: { tag_id: TAG.MOVED } }),
    event(4, '2026-11-02T09:00:00Z', 'tag_bound', { data: { tag_id: TAG.BLACK } }),
    event(4, '2026-11-02T09:10:00Z', 'loaded', { ...printer, source: 'person' }),
    event(19, '2026-11-10T12:00:00Z', 'tag_unbound', { data: { tag_id: TAG.MOVED, reason: 'spool empty' } }),
    event(19, '2026-11-10T12:01:00Z', 'archived'),
    event(3, '2026-11-10T12:05:00Z', 'tag_bound', { data: { tag_id: TAG.MOVED, override: true } }),
    event(4, '2026-11-14T19:00:00Z', 'print_started', { ...printer, ref_kind: 'print', ref_id: '35', actor: null }),
    event(4, '2026-11-14T21:30:00Z', 'print_ended', { ...printer, source: 'print_actual', grams_before: 268, grams_after: 120, delta_g: -148, ref_kind: 'print', ref_id: '35', actor: null }),
    // Dated before every other event, so the newest-first ledger keeps its order. A plate both SB (a print run) and BB (a usage row) booked, and a print still running: the suppliers stats count 20 g and 0 g (`metrics.plates`).
    event(3, '2026-10-30T10:00:00Z', 'print_ended', { source: 'print_actual', delta_g: -20, ref_kind: 'print_run', ref_id: 'run1/job-a/0', actor: null, data: { job: 'job-a' } }),
    event(3, '2026-10-30T10:01:00Z', 'print_ended', { source: 'print_actual', delta_g: -20, ref_kind: 'bb_usage', ref_id: 'usage-a', actor: null, data: { job: 'job-a' } }),
    event(10, '2026-10-30T11:00:00Z', 'print_started', { source: 'print_estimate', delta_g: -30, ref_kind: 'print_run', ref_id: 'run2/job-b/0', actor: null, data: { job: 'job-b' } }),
    // #7682: a weighing with its breakdown: the reading, the insert taken off, what the ledger expected.
    event(1, '2026-11-14T18:00:00Z', 'weighed', { source: 'scale', grams_before: 655, grams_after: 640, delta_g: -15, data: { reading_g: 890, empty_g: 250, empty_source: 'insert', empty_basis: 'catalog', empty_name: 'Bambu reusable spool', expected_g: 652, drift_g: -12 } }),
    // #6269 — #2's life, for its timeline and grams chart: readings the grams anchor on, a
    // Bambuddy plate with its print (archive 41), a plate of an order's run, a failed plate,
    // a move, an assistant's edit and a person's reset, a type's edit, a drying, and an AMS
    // reading paired with a scale reading (+20 g). The plates have the shape the API books
    // (`print_actual`, `delta_g` only, `bambuddy_usage._book`), and the seqs follow time, as
    // the grams series reads the value after an event by seq. All before #1's weighing, so
    // the ledger's newest rows stay as they were.
    event(2, '2026-11-02T08:00:00Z', 'weighed', { source: 'scale', grams_before: 1000, grams_after: 995, delta_g: -5 }),
    event(2, '2026-11-03T20:00:00Z', 'print_ended', { ...printer, source: 'print_actual', grams_before: 995, delta_g: -55, ref_kind: 'bb_usage', ref_id: 'usage-c', actor: 'bambuddy', data: { job: 'job-c', basis: 'bb_usage', bb_archive_id: 41, bb_status: 'completed' } }),
    event(2, '2026-11-05T09:30:00Z', 'moved', { grams_before: 940, data: { from: L.SHELF_A, to: L.DRY_BOX } }),
    event(2, '2026-11-06T16:00:00Z', 'print_ended', { ...printer, source: 'print_actual', grams_before: 940, delta_g: -35, ref_kind: 'print_run', ref_id: 'run3/job-d/0', actor: 'system:printer-watch', data: { job: 'job-d', basis: 'sliced', length_m: 11.7 } }),
    event(2, '2026-11-09T11:00:00Z', 'print_ended', { ...printer, source: 'print_actual', grams_before: 905, delta_g: -30, ref_kind: 'bb_usage', ref_id: 'usage-e', actor: 'bambuddy', data: { job: 'job-e', basis: 'bb_usage', bb_archive_id: 42, bb_status: 'failed' } }),
    event(2, '2026-11-10T14:00:00Z', 'edited', { grams_before: 875, actor: 'service:agent', data: { changes: { cost_per_kg: [null, '25.0000'] }, author: { principal: 'person@example.com', session: 'mock-session', tool: 'filament_update_spool' } } }),
    event(2, '2026-11-11T09:00:00Z', 'edited', { grams_before: 875, data: { changes: { cost_per_kg: ['25.0000', null] } } }),
    event(2, '2026-11-11T12:00:00Z', 'edited', { grams_before: 875, data: { changes: { nozzle_temp_max: [250, 260] }, type_id: T.ORANGE } }),
    event(2, '2026-11-12T17:00:00Z', 'dried', { source: 'person', grams_before: 875, data: { temp_c: 65, hours: '8' } }),
    event(2, '2026-11-12T21:00:00Z', 'ams_reading', { ...printer, source: 'ams_remain', grams_before: 875, grams_after: 840, delta_g: -35, actor: 'bambuddy', data: { remain_pct: 84 } }),
    event(2, '2026-11-13T09:00:00Z', 'weighed', { source: 'scale', grams_before: 840, grams_after: 820, delta_g: -20 }),
  ]
}

/** A spool's own preset row (`filament_spool_presets`); the spec carries only the effective one. */
/** `migrated` marks a row the #6262 migration made from a spool's old preset: its first type drops it (`presets.drop_migrated`). */
export type SpoolPreset = Omit<S['TypePreset'], 'type_id'> & { spool_id: string; migrated?: boolean }
/** A spool's own K value row (`filament_spool_k_values`). */
export type SpoolKValue = Omit<S['TypeKValue'], 'type_id' | 'source'> & { spool_id: string }

/** The whole seed, fresh: `index.ts`'s `reset` takes a new one. */
export function seed() {
  return {
    catalogs: catalogs(),
    locations: locations(),
    types: types(),
    typePresets: typePresets(),
    typeKValues: typeKValues(),
    spoolPresets: [
      { spool_id: spoolId(6), printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'GFA00', slicer_filament_name: 'Bambu PLA Basic @BBL H2C', migrated: true },
    ] as SpoolPreset[],
    spoolKValues: [] as SpoolKValue[],
    spools: spools(),
    tags: tags(),
    bindings: bindings(),
    events: events(),
    sensors: sensors(),
    /** #6269 — the order whose item a print run fulfilled (`order_item_fulfilments.print_run_id`), by run id. */
    orderRuns: new Map<string, string>([['run3', ORDER_CLOSED]]),
    /** The Bambuddy spool each SB spool is linked to (#7 came from Bambuddy spool 41). */
    bbLinks: new Map<string, number>([[spoolId(7), 41]]),
  }
}

/** The sensors the places carry (#6268): a shelf that reads too damp, a dry box, and an AMS tray's own. */
export const SENSOR = { SHELF_HUMIDITY: sensorId(1), BOX_HUMIDITY: sensorId(2), BOX_TEMP: sensorId(3), AMS_HUMIDITY: sensorId(4) }

function sensors(): S['Sensor'][] {
  const sensor = (n: number, location_id: string, values: Partial<S['Sensor']>): S['Sensor'] => ({
    id: sensorId(n),
    location_id,
    name: '',
    source: 'home_assistant',
    kind: 'humidity',
    entity_id: null,
    printer_id: null,
    ams_id: null,
    unit: '%',
    alert_above: null,
    alert_below: null,
    notify: false,
    last_value: null,
    last_at: null,
    alerting: false,
    reachable: true,
    archived_at: null,
    created_at: AT,
    last_notification: null,
    ...values,
  })
  // #6646: the shelf's alert went out through Bambuddy; the dry box's end did not (the key lacks the scope).
  const notified = (values: Partial<S['SensorNotification']>): S['SensorNotification'] => ({
    kind: 'started',
    value: 0,
    read_at: '2026-11-18T09:00:00Z',
    channel: 'bambuddy',
    state: 'sent',
    channels: 2,
    error: null,
    queued_at: '2026-11-18T09:00:00Z',
    sent_at: '2026-11-18T09:05:00Z',
    ...values,
  })
  return [
    sensor(1, L.SHELF_A, {
      name: 'Shelf A humidity',
      entity_id: 'sensor.shelf_a_humidity',
      alert_above: 55,
      notify: true,
      last_value: 62,
      last_at: '2026-11-18T09:45:00Z',
      alerting: true,
      last_notification: notified({ value: 57 }),
    }),
    sensor(2, L.DRY_BOX, {
      name: 'Dry box humidity',
      entity_id: 'sensor.dry_box_humidity',
      alert_above: 20,
      notify: true,
      last_value: 11,
      last_at: '2026-11-18T09:45:00Z',
      last_notification: notified({ kind: 'stopped', value: 18, state: 'failed', channels: null, error: 'scope' }),
    }),
    sensor(3, L.DRY_BOX, { name: 'Dry box temperature', kind: 'temperature', entity_id: 'sensor.dry_box_temperature', unit: '°C', last_value: 38.5, last_at: '2026-11-18T09:45:00Z', reachable: false }),
    sensor(4, L.AMS_A, { name: 'AMS-A humidity', source: 'ams', printer_id: H2C.printer_id, ams_id: 0, last_value: 18, last_at: '2026-11-18T09:40:00Z' }),
  ]
}

export type Seed = ReturnType<typeof seed>
