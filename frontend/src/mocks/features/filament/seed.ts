import type { components } from '../../../api/schema'
import type { FilamentSpool } from '../../../api/types'

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
    last_dried_at: null,
    last_dried_temp_c: null,
    last_dried_hours: null,
    last_used_at: null,
    archived_at: look.archived ? AT : null,
    created_at: AT,
    updated_at: AT,
    low_stock: false,
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

function material(n: number, name: string, values: Partial<S['Material']> = {}): S['Material'] {
  return {
    ...row(n, name),
    family: null,
    density_g_cm3: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    bed_temp_min: null,
    bed_temp_max: null,
    drying_temp_c: null,
    drying_hours: null,
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
    ...row(n, name),
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
export const LK = { ROOM: catalogId(80), SHELF: catalogId(81), DRY_BOX: catalogId(82), AMS: catalogId(83) }

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
      material(2, 'PETG', { family: 'PETG', density_g_cm3: '1.27', nozzle_temp_min: 230, nozzle_temp_max: 260, bed_temp_min: 65, bed_temp_max: 80, drying_temp_c: 65, drying_hours: '8', hygroscopic: true }),
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
      row(20, 'Bambu Lab'),
      row(21, 'Insignia'),
      row(22, 'Inland'),
      row(23, 'Polymaker'),
      row(24, 'Elegoo'),
      row(25, 'Sunlu'),
    ],
    categories: [row(30, 'Standard'), row(31, 'Engineering', 1)],
    effects: [row(40, 'Translucent'), row(41, 'Clear', 1), row(42, 'Silk', 2)],
    'insert-materials': [row(50, 'Plastic'), row(51, 'Cardboard', 1)],
    'insert-types': [
      { ...row(60, 'Bambu reusable spool'), empty_weight_g: 250, insert_material_id: catalogId(50), note: null, bb_catalog_id: null },
      { ...row(61, 'Cardboard spool', 1), empty_weight_g: 180, insert_material_id: catalogId(51), note: null, bb_catalog_id: null },
    ],
    colours: [
      { ...row(70, 'Jade White'), manufacturer: 'Bambu Lab', rgba: '#FFFFFFFF', material_id: M.PLA, extra_colours: null, effect_id: null },
      { ...row(71, 'Hot Pink'), manufacturer: 'Bambu Lab', rgba: '#F5547CFF', material_id: M.PLA, extra_colours: null, effect_id: null },
      { ...row(72, 'Translucent Orange'), manufacturer: 'Bambu Lab', rgba: '#FF6A1380', material_id: M.PETG, extra_colours: null, effect_id: E.TRANSLUCENT },
    ],
    'location-kinds': [
      { ...row(80, 'Room'), dry_storage: false, ams: false },
      { ...row(81, 'Shelf', 1), dry_storage: false, ams: false },
      { ...row(82, 'Dry box', 2), dry_storage: true, ams: false },
      { ...row(83, 'AMS tray', 3), dry_storage: false, ams: true },
    ],
  }
}

// -- places ---------------------------------------------------------------------------

export const L = { WORKSHOP: locationId(1), SHELF_A: locationId(2), DRY_BOX: locationId(3), AMS_A1: locationId(4), AMS_A4: locationId(5) }

function place(n: number, name: string, kind_id: string, values: Partial<S['Location']> = {}): S['Location'] {
  return {
    ...row(n, name),
    id: locationId(n),
    kind_id,
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
    place(4, 'H2C AMS-A slot 1', LK.AMS, { printer_id: H2C.printer_id, ams_id: 0, tray_id: 0, sort_order: 2 }),
    place(5, 'H2C AMS-A slot 4', LK.AMS, { printer_id: H2C.printer_id, ams_id: 0, tray_id: 3, sort_order: 3 }),
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
}

function spoolType(look: TypeLook): S['SpoolType'] {
  return {
    id: typeId(look.n),
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
    label_weight_g: look.label_weight_g ?? 1000,
    insert_type_id: look.insert ?? IT.BAMBU_REUSABLE,
    low_stock_pct: null,
    cost_per_kg: look.cost_per_kg ?? null,
    filament_id: null,
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
    spoolType({ n: 2, code: 'BL-PETG-HF-TRANSLUCENT-ORANGE', brand: B.BAMBU, material: M.PETG, subtype: ST.HF, effect: E.TRANSLUCENT, colour_name: 'Translucent Orange', rgba: '#FF6A1380', cost_per_kg: '19.99' }),
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
  { type_id: T.JADE, printer_id: H2C.printer_id, extruder: 0, nozzle_diameter: '0.4', nozzle_type: 'hardened_steel', k_value: '0.020', name: 'Jade White 0.4', cali_idx: 3, setting_id: null },
]

// -- spools ---------------------------------------------------------------------------

/** A spool's own columns, as `filament_spools` holds them: an override, or `null`. */
export type SpoolRow = Omit<FilamentSpool, 'effective' | 'overridden' | 'names' | 'low_stock'>

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
    grams_left: null,
    last_weighed_g: null,
    last_weighed_at: null,
    last_dried_at: null,
    last_dried_temp_c: null,
    last_dried_hours: null,
    last_used_at: null,
    archived_at: null,
    created_at: AT,
    updated_at: AT,
    ...values,
  }
}

function spools(): SpoolRow[] {
  return [
    spool(1, { type_id: T.JADE, grams_left: 640, location_id: L.SHELF_A, last_weighed_g: 640, last_weighed_at: '2026-11-14T18:00:00Z' }),
    spool(2, { type_id: T.ORANGE, grams_left: 820, location_id: L.DRY_BOX, last_dried_at: '2026-11-12T09:00:00Z', last_dried_temp_c: 65, last_dried_hours: '8' }),
    spool(3, { type_id: T.PINK, grams_left: 930, location_id: L.SHELF_A }),
    spool(4, { type_id: T.BLACK, grams_left: 120, location_id: L.AMS_A1, last_used_at: '2026-11-14T21:30:00Z' }),
    spool(5, { type_id: T.POLY_CLEAR, grams_left: 700, location_id: L.DRY_BOX }),
    // Added by hand with only its material: it needs a type.
    spool(6, { material_id: M.PLA, colour_name: 'Teal', rgba: '#1B9AAAFF', label_weight_g: 1000, grams_left: 500 }),
    // Pulled from Bambuddy with no material, and no label weight: no grams arc.
    spool(7, { material_id: M.UNKNOWN, rgba: '#808080FF', grams_left: 300, note: 'Pulled from Bambuddy spool 41.' }),
    spool(8, { type_id: T.INSIGNIA_CLEAR, grams_left: 400, location_id: L.AMS_A4 }),
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
    ...spools().map((row) => event(row.number, '2026-11-01T09:00:00Z', 'created')),
    event(19, '2026-11-01T10:00:00Z', 'tag_bound', { data: { tag_id: TAG.MOVED } }),
    event(4, '2026-11-02T09:00:00Z', 'tag_bound', { data: { tag_id: TAG.BLACK } }),
    event(4, '2026-11-02T09:10:00Z', 'loaded', { ...printer, source: 'person' }),
    event(19, '2026-11-10T12:00:00Z', 'tag_unbound', { data: { tag_id: TAG.MOVED, reason: 'spool empty' } }),
    event(19, '2026-11-10T12:01:00Z', 'archived'),
    event(3, '2026-11-10T12:05:00Z', 'tag_bound', { data: { tag_id: TAG.MOVED, override: true } }),
    event(2, '2026-11-12T17:00:00Z', 'dried', { source: 'person', data: { temp_c: 65, hours: '8' } }),
    event(4, '2026-11-14T19:00:00Z', 'print_started', { ...printer, ref_kind: 'print', ref_id: '35', actor: null }),
    event(4, '2026-11-14T21:30:00Z', 'print_ended', { ...printer, source: 'print_actual', grams_before: 268, grams_after: 120, delta_g: -148, ref_kind: 'print', ref_id: '35', actor: null }),
    event(1, '2026-11-14T18:00:00Z', 'weighed', { source: 'scale', grams_before: 655, grams_after: 640, delta_g: -15 }),
  ]
}

/** A spool's own preset row (`filament_spool_presets`); the spec carries only the effective one. */
/** `migrated` marks a row the #6262 migration made from a spool's old preset: its first type drops it (`presets.drop_migrated`). */
export type SpoolPreset = Omit<S['TypePreset'], 'type_id'> & { spool_id: string; migrated?: boolean }
/** A spool's own K value row (`filament_spool_k_values`). */
export type SpoolKValue = Omit<S['TypeKValue'], 'type_id'> & { spool_id: string }

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
    /** The Bambuddy spool each SB spool is linked to (#7 came from Bambuddy spool 41). */
    bbLinks: new Map<string, number>([[spoolId(7), 41]]),
  }
}

export type Seed = ReturnType<typeof seed>
