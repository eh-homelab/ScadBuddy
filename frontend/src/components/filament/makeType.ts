import {
  listCatalog,
  patchSpool,
  setSpoolKValues,
  setSpoolPresets,
  setTypeKValues,
  setTypePresets,
  upsertCatalog,
  upsertType,
  type CatalogKind,
  type CatalogRows,
  type SpoolDetail,
  type SpoolPatch,
  type SpoolType,
} from '../../api/filament'
import type { Filament } from '../../api/types'

const same = (a: string, b: string) => a.trim().toUpperCase() === b.trim().toUpperCase()

/** What `makeTypeFromBook` added to the catalog, then the refusal of the type itself. */
export class MakeTypeError extends Error {
  override cause: unknown
  added: string[]
  constructor(cause: unknown, added: string[]) {
    super('the type was refused')
    this.cause = cause
    this.added = added
  }
}

/**
 * The row of `kind` named `name` (and, for a subtype, of `materialId`: a subtype's name is
 * unique per material), added when it is missing, as the Bambuddy pull and the CSV import
 * add one. `added` collects what this call made.
 */
async function named<K extends CatalogKind>(kind: K, name: string, added: string[], materialId?: string): Promise<CatalogRows[K]> {
  const rows = await listCatalog(kind)
  const found = rows.find((row) => same(row.name, name) && (materialId === undefined || (row as { material_id?: string | null }).material_id === materialId))
  if (found) return found
  const made = await upsertCatalog(kind, { name, ...(materialId ? { material_id: materialId } : {}) } as Parameters<typeof upsertCatalog<K>>[1])
  added.push(name)
  return made
}

/** A type's code from a book row's identity: `BAMBU-PLA-BASIC-JADE-WHITE`. */
export function typeCodeOf(row: Pick<Filament, 'brand' | 'material' | 'subtype' | 'colour_name' | 'colour'>): string {
  return [row.brand, row.material, row.subtype, row.colour_name ?? row.colour]
    .filter(Boolean)
    .join('-')
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
}

/**
 * #6262 — "Make a type": a price book row's material, subtype, brand, colour (with its
 * alpha, as `#RRGGBBAA`), effect and cost become a spool type, the identity the row
 * already has. The server links the new type to that row (`filament_id`), since the type
 * has the row's identity. A name the catalog lacks is added first (the type needs its id),
 * and stays if the type is then refused: the refusal is a `MakeTypeError` that lists them.
 */
export async function makeTypeFromBook(row: Filament): Promise<SpoolType> {
  const added: string[] = []
  const material = await named('materials', row.material, added)
  const brand = row.brand ? await named('brands', row.brand, added) : null
  const subtype = row.subtype ? await named('subtypes', row.subtype, added, material.id) : null
  const effect = row.effect ? ((await listCatalog('effects')).find((one) => same(one.name, row.effect!.replace(/-/g, ' '))) ?? null) : null
  const alpha = row.alpha.toString(16).padStart(2, '0')
  return upsertType({
    code: typeCodeOf(row),
    material_id: material.id,
    subtype_id: subtype?.id ?? null,
    brand_id: brand?.id ?? null,
    effect_id: effect?.id ?? null,
    colour_name: row.colour_name,
    rgba: `${row.colour}${alpha}`.toUpperCase(),
    extra_colours: row.extra_colours.length > 0 ? row.extra_colours : null,
    density_g_cm3: row.density_g_cm3,
    cost_per_kg: row.cost_per_kg,
    diameter_mm: '1.75',
    label_weight_g: 1000,
  }).catch((cause: unknown) => {
    throw new MakeTypeError(cause, added)
  })
}

/** The spool fields a type holds: once a spool has the type they are the type's, so the spool's own are cleared. */
const TYPE_FIELDS = [
  'brand_id',
  'material_id',
  'subtype_id',
  'category_id',
  'effect_id',
  'colour_name',
  'rgba',
  'extra_colours',
  'diameter_mm',
  'density_g_cm3',
  'nozzle_temp_min',
  'nozzle_temp_max',
  'bed_temp_min',
  'bed_temp_max',
  'label_weight_g',
  'insert_type_id',
  'low_stock_pct',
  'cost_per_kg',
] as const satisfies readonly (keyof SpoolPatch)[]

/**
 * #6262 — "Create type from this spool": a spool that has no type becomes the first of
 * one. The type takes the spool's effective values, its own presets and K values; the
 * spool then takes the type (`type_id`), drops what the type now holds, and keeps no
 * preset or K value of its own. A material and a colour are the least a type needs. The
 * type's code is `BRAND-MATERIAL-SUBTYPE-COLOUR`; a code another type has is the server's
 * refusal (it names it), which the caller shows.
 */
export async function makeTypeFromSpool(spool: SpoolDetail): Promise<SpoolType> {
  const e = spool.effective
  if (!e.material_id) throw new Error('Choose the material first.')
  if (!e.rgba) throw new Error('Choose the colour first.')
  const code = typeCodeOf({
    brand: spool.names.brand ?? null,
    material: spool.names.material ?? '',
    subtype: spool.names.subtype ?? null,
    colour_name: e.colour_name ?? null,
    colour: e.rgba.slice(0, 7),
  })
  const type = await upsertType({
    code,
    material_id: e.material_id,
    subtype_id: e.subtype_id ?? null,
    brand_id: e.brand_id ?? null,
    category_id: e.category_id ?? null,
    effect_id: e.effect_id ?? null,
    insert_type_id: e.insert_type_id ?? null,
    rgba: e.rgba,
    colour_name: e.colour_name ?? null,
    extra_colours: e.extra_colours ?? null,
    diameter_mm: e.diameter_mm ?? '1.75',
    density_g_cm3: e.density_g_cm3 ?? null,
    cost_per_kg: e.cost_per_kg ?? null,
    label_weight_g: e.label_weight_g ?? 1000,
    low_stock_pct: e.low_stock_pct ?? null,
    nozzle_temp_min: e.nozzle_temp_min ?? null,
    nozzle_temp_max: e.nozzle_temp_max ?? null,
    bed_temp_min: e.bed_temp_min ?? null,
    bed_temp_max: e.bed_temp_max ?? null,
  })
  // Only the input fields: an effective row also carries `overridden` and `migrated`, which the server refuses.
  const presets = spool.presets
    .filter((row) => row.overridden)
    .map(({ printer_model, nozzle_diameter, slicer_filament, slicer_filament_name }) => ({ printer_model, nozzle_diameter, slicer_filament, slicer_filament_name }))
  const kValues = spool.k_values
    .filter((row) => row.overridden)
    .map(({ printer_id, extruder, nozzle_diameter, nozzle_type, k_value, name, cali_idx, setting_id }) => ({ printer_id, extruder, nozzle_diameter, nozzle_type, k_value, name, cali_idx, setting_id }))
  if (presets.length > 0) await setTypePresets(type.id, presets)
  if (kValues.length > 0) await setTypeKValues(type.id, kValues)
  await patchSpool(spool.id, { type_id: type.id, ...Object.fromEntries(TYPE_FIELDS.map((field) => [field, null])) })
  // The type holds them now; the first type may already have dropped migrated ones.
  if (presets.length > 0) await setSpoolPresets(spool.id, [])
  if (kValues.length > 0) await setSpoolKValues(spool.id, [])
  return type
}
