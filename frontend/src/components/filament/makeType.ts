import { ApiError } from '../../api/client'
import {
  bulkUpdateSpools,
  resolveCatalog,
  setSpoolKValues,
  setSpoolPresets,
  setTypeCost,
  setTypeKValues,
  setTypePresets,
  upsertCatalog,
  upsertType,
  type AliasedKind,
  type CatalogRows,
  type SpoolDetail,
  type SpoolPatch,
  type SpoolType,
} from '../../api/filament'
import type { Filament } from '../../api/types'
import type { PropertyValue } from '../entities/api'

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
 * #6706 — the row of `kind` whose name or alias has `name`'s key (the server's
 * `GET /catalog/{kind}/resolve`: any case and punctuation; a subtype is `materialId`'s own,
 * else one for every material), or `null` for none.
 */
async function resolved<K extends AliasedKind>(kind: K, name: string, materialId?: string): Promise<CatalogRows[K] | null> {
  try {
    return await resolveCatalog(kind, name, materialId)
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 404) return null
    throw cause
  }
}

/**
 * The row of `kind` that `name` resolves to, added when there is none, as the Bambuddy
 * pull and the CSV import add one (a subtype of `materialId`). `added` collects what this
 * call made.
 */
async function named<K extends AliasedKind>(kind: K, name: string, added: string[], materialId?: string): Promise<CatalogRows[K]> {
  const found = await resolved(kind, name, materialId)
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
export async function makeTypeFromBook(row: Filament, properties?: Record<string, PropertyValue>): Promise<SpoolType> {
  const added: string[] = []
  const material = await named('materials', row.material, added)
  const brand = row.brand ? await named('brands', row.brand, added) : null
  const subtype = row.subtype ? await named('subtypes', row.subtype, added, material.id) : null
  // The book's `dual-color` is the catalog's `Dual Color` by key; an effect is never added.
  const effect = row.effect ? await resolved('effects', row.effect) : null
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
    ...(properties && Object.keys(properties).length > 0 ? { properties } : {}),
  }).catch((cause: unknown) => {
    throw new MakeTypeError(cause, added)
  })
}

/**
 * #7159 — the row's catalog ids as `resolveCatalog` finds them by key and alias. `null` is a
 * name the row leaves empty; `false` is a name the catalog lacks, which no type can hold.
 * An effect the catalog lacks is `null`, as `makeTypeFromBook` leaves it empty.
 */
export interface BookRowIds {
  brandId: string | null | false
  materialId: string | false
  subtypeId: string | null | false
  effectId: string | null
}

/** What of a candidate is not the row's. */
export type CandidateDiff = 'material' | 'subtype' | 'effect' | 'alpha'

/** A type the row may already be: `exact` when its material, subtype, colour (alpha too) and effect are the row's; else `differs` names the rest. */
export interface TypeCandidate {
  type: SpoolType
  exact: boolean
  differs: CandidateDiff[]
}

const rgbOf = (hex: string) => hex.slice(0, 7).toUpperCase()

/**
 * #7159 R-7159-1, R-7159-2 — the unarchived types of the row's brand and RGB (the alpha
 * ignored), whatever their material, subtype or effect; the exact matches first, then by code.
 */
export function rankCandidates(row: Pick<Filament, 'colour' | 'alpha'>, ids: BookRowIds, types: readonly SpoolType[]): TypeCandidate[] {
  const alpha = row.alpha.toString(16).padStart(2, '0').toUpperCase()
  return types
    .filter((type) => !type.archived_at && type.brand_id === ids.brandId && rgbOf(type.rgba) === rgbOf(row.colour))
    .map((type) => {
      const differs: CandidateDiff[] = []
      if (type.material_id !== ids.materialId) differs.push('material')
      if (type.subtype_id !== ids.subtypeId) differs.push('subtype')
      if (type.effect_id !== ids.effectId) differs.push('effect')
      if (type.rgba.slice(7, 9).toUpperCase() !== alpha) differs.push('alpha')
      return { type, exact: differs.length === 0, differs }
    })
    .sort((a, b) => Number(b.exact) - Number(a.exact) || a.type.code.localeCompare(b.type.code))
}

/**
 * #7159 — whether setting `type`'s cost links `row`: the server finds the type's book row
 * by its material, subtype and brand names (upper-cased, no aliases), its RGB and its
 * alpha, and not its effect (`types.py` `_link_price_book`, `orders/store.py`
 * `existing_filament`). `names` holds the catalog names by id; an id it lacks never links.
 * It is the dialog's forecast; the PATCH answer's `filament_id` says what the server linked (#7344).
 */
export function linksBookRow(row: Pick<Filament, 'material' | 'subtype' | 'brand' | 'colour' | 'alpha'>, type: SpoolType, names: ReadonlyMap<string, string>): boolean {
  const name = (id: string | null) => (id === null ? '' : (names.get(id) ?? null))
  const same = (text: string | null, id: string | null) => {
    const held = name(id)
    return held !== null && (text ?? '').toUpperCase() === held.toUpperCase()
  }
  const alpha = row.alpha.toString(16).padStart(2, '0').toUpperCase()
  return (
    same(row.material, type.material_id) &&
    same(row.subtype, type.subtype_id) &&
    same(row.brand, type.brand_id) &&
    rgbOf(type.rgba) === rgbOf(row.colour) &&
    type.rgba.slice(7, 9).toUpperCase() === alpha
  )
}

/** #7159 — the row's brand, material, subtype and effect, resolved by key and alias; it adds no catalog row. */
export async function bookRowIds(row: Pick<Filament, 'brand' | 'material' | 'subtype' | 'effect'>): Promise<BookRowIds> {
  const [brand, material, effect] = await Promise.all([
    row.brand ? resolved('brands', row.brand) : null,
    resolved('materials', row.material),
    row.effect ? resolved('effects', row.effect) : null,
  ])
  const subtype = row.subtype && material ? await resolved('subtypes', row.subtype, material.id) : null
  return {
    brandId: row.brand ? (brand?.id ?? false) : null,
    materialId: material?.id ?? false,
    subtypeId: row.subtype ? (subtype?.id ?? false) : null,
    effectId: effect?.id ?? null,
  }
}

/**
 * #7159 R-7159-4 — link an existing type instead of making one: only its `cost_per_kg` is
 * set, from the row. The server then links the book row of the type's own identity, and
 * gives that row this cost (`linksBookRow` forecasts whether it is this row). When it is another
 * row, the server adds it if the book has none. The answer's `filament_id` says which.
 */
export function linkTypeToBook(type: SpoolType, row: Pick<Filament, 'cost_per_kg'>): Promise<SpoolType> {
  // The cost route (#7703): a type its required keys leave incomplete still takes a price.
  return setTypeCost(type.id, row.cost_per_kg)
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
export async function makeTypeFromSpool(spool: SpoolDetail, properties?: Record<string, PropertyValue>): Promise<SpoolType> {
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
    ...(properties && Object.keys(properties).length > 0 ? { properties } : {}),
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
  // The unchecked bulk route (#7703): a spool its required keys leave incomplete still takes the type.
  await bulkUpdateSpools([spool.id], { type_id: type.id, ...Object.fromEntries(TYPE_FIELDS.map((field) => [field, null])) })
  // The type holds them now; the first type may already have dropped migrated ones.
  if (presets.length > 0) await setSpoolPresets(spool.id, [])
  if (kValues.length > 0) await setSpoolKValues(spool.id, [])
  return type
}
