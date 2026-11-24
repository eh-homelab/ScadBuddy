import type { BackendClient } from '../api/backend.js'
import type { components } from '../api/schema.js'
import { ok } from './call.js'

/**
 * #6894 — the filament text search of `filament_spool_list` and `filament_type_list` `q`, so
 * the assistant finds a spool the way the Filament page does. A copy of the frontend's
 * `frontend/src/lib/filamentSearch.ts`; change both together (both tests run
 * `test-fixtures/filament/search-vectors.json`). The query is split on white space, and every token must match some
 * field, in any case:
 *
 * - `#87` (a `#` and 1 to 5 digits) is a spool number, and matches spool 87 only.
 * - Any other token matches a spool whose number it is (`87`), a text field that contains it
 *   (brand, material, colour name, SKU, place…), or, from 3 characters, a colour whose hex
 *   contains it, with or without its `#`.
 * - #7151: from 4 hex digits (`:` and `-` left out), a tag UID or tray UUID that holds it.
 * - A spool's or type's property values (#7026) are text fields too (a GTIN, an MPN, a lot);
 *   a key's name is not.
 */
type Spool = components['schemas']['Spool']
type SpoolType = components['schemas']['SpoolType']

export interface SearchToken {
  text: string
  numberOnly: boolean
}

export interface SearchDoc {
  number?: number | null
  texts: readonly (string | null | undefined)[]
  hexes: readonly (string | null | undefined)[]
  tags?: readonly string[]
}

export function parseSearch(query: string | null | undefined): SearchToken[] {
  return (query ?? '')
    .toLowerCase()
    .split(/\s+/)
    .filter((raw) => raw !== '' && raw !== '#')
    .map((raw) => ({ text: raw.replace(/^#/, ''), numberOnly: /^#\d{1,5}$/.test(raw) }))
}

const isNumber = (text: string, number: number | null | undefined) => number != null && /^\d+$/.test(text) && Number(text) === number

function matchesToken(doc: SearchDoc, token: SearchToken): boolean {
  if (isNumber(token.text, doc.number)) return true
  if (token.numberOnly) return false
  if (doc.texts.some((text) => text?.toLowerCase().includes(token.text))) return true
  if (token.text.length >= 3 && /^[0-9a-f]+$/.test(token.text) && doc.hexes.some((hex) => hex?.toLowerCase().replace(/^#/, '').includes(token.text))) return true
  // #7151: a tag key as a reader prints it, `04:A1:B2` or `04-a1-b2`, is the same hex.
  const key = token.text.replace(/[:-]/g, '')
  return key.length >= 4 && /^[0-9a-f]+$/.test(key) && (doc.tags ?? []).some((tag) => tag.toLowerCase().includes(key))
}

export function matchesSearch(doc: SearchDoc, tokens: readonly SearchToken[]): boolean {
  return tokens.every((token) => matchesToken(doc, token))
}

export interface SpoolExtras {
  typeCode?: string | null
  skus?: readonly string[]
  tray?: string | null
  /** #7151 — its open tag's UID and tray UUID. */
  tags?: readonly string[]
  /** The spool's property values as text (`propertyTexts`). */
  properties?: readonly string[]
}

/** What else a type is searched by: its property values as text (`propertyTexts`). */
export interface TypeExtras {
  properties?: readonly string[]
}

/**
 * An entity's property values as search text: a string as it is, each list item, a number as text.
 * A boolean is left out: "true" would match every entity with any yes/no property.
 */
export function propertyTexts(values: Readonly<Record<string, string | number | boolean | readonly string[]>> | undefined): string[] {
  return Object.values(values ?? {}).flatMap((value) => (Array.isArray(value) ? value : typeof value === 'boolean' ? [] : [String(value)]))
}

export function spoolSearchDoc(spool: Spool, extras: SpoolExtras = {}): SearchDoc {
  const names = spool.names ?? {}
  return {
    number: spool.number,
    texts: [
      names.brand,
      names.material,
      names.subtype,
      spool.effective?.colour_name ?? spool.colour_name,
      names.location,
      extras.typeCode,
      extras.tray,
      ...(extras.skus ?? []),
      ...(extras.properties ?? []),
    ],
    hexes: [spool.effective?.rgba ?? spool.rgba],
    tags: extras.tags ?? [],
  }
}

export function typeSearchDoc(type: SpoolType, names: ReadonlyMap<string, string>, skus: readonly string[] = [], extras: TypeExtras = {}): SearchDoc {
  const name = (id: string | null | undefined) => (id ? names.get(id) : null)
  return {
    texts: [type.code, name(type.brand_id), name(type.material_id), name(type.subtype_id), type.colour_name, ...skus, ...(extras.properties ?? [])],
    hexes: [type.rgba],
  }
}

/** The SKUs, catalog and property routes the search reads, for a tool's `routes`. */
export const SEARCH_ROUTES = [
  'GET /api/v1/filament/suppliers',
  'GET /api/v1/filament/suppliers/{supplier_id}/products',
  'GET /api/v1/entity-kinds/{kind}/properties',
] as const

/**
 * #7026: each `kind` entity's property values as search text, by its id (the read keys each
 * entity by its ref within the kind, the spool's or type's id), for those that have properties.
 */
export async function kindPropertyTexts(backend: BackendClient, kind: 'spool' | 'spool_type'): Promise<Map<string, string[]>> {
  const { properties } = await ok(backend.GET('/api/v1/entity-kinds/{kind}/properties', { params: { path: { kind } } }), `read the ${kind} properties`)
  return new Map(Object.entries(properties).map(([id, values]) => [id, propertyTexts(values)]))
}

/**
 * Each type's SKUs, from the products of every supplier not archived. A supplier whose
 * products cannot be read leaves its SKUs out.
 */
export async function typeSkus(backend: BackendClient): Promise<Map<string, string[]>> {
  const skus = new Map<string, string[]>()
  const suppliers = await ok(backend.GET('/api/v1/filament/suppliers', { params: { query: { include_archived: false } } }), 'list the suppliers')
  const lists = await Promise.allSettled(
    suppliers.map((supplier) =>
      ok(
        backend.GET('/api/v1/filament/suppliers/{supplier_id}/products', {
          params: { path: { supplier_id: supplier.id }, query: { include_archived: false } },
        }),
        'list the products',
      ),
    ),
  )
  for (const list of lists) {
    if (list.status !== 'fulfilled') continue
    for (const product of list.value) {
      const have = skus.get(product.type_id) ?? []
      if (!have.includes(product.sku)) skus.set(product.type_id, [...have, product.sku])
    }
  }
  return skus
}

/** The tray each spool is in, "H2C AMS-A slot 1", from the printers' latest reports. */
export async function spoolTrays(backend: BackendClient): Promise<Map<string, string>> {
  const view = await ok(backend.GET('/api/v1/filament/inventory', { params: { query: {} } }), 'read the trays')
  const found = new Map<string, string>()
  for (const printer of view.trays) {
    for (const tray of printer.trays) if (tray.spool_id) found.set(tray.spool_id, `${printer.printer_name} ${tray.label}`)
  }
  return found
}

/** #7151 — each spool's open tag keys (its UID and tray UUID), for the search by tag. */
export async function spoolTags(backend: BackendClient): Promise<Map<string, string[]>> {
  const tags = await ok(backend.GET('/api/v1/filament/tags', { params: { query: {} } }), 'list the tags')
  const found = new Map<string, string[]>()
  for (const tag of tags) {
    if (!tag.binding) continue
    found.set(tag.binding.spool_id, [tag.tag_uid, tag.tray_uuid].filter((key): key is string => !!key))
  }
  return found
}

/** Catalog row id to name, for the materials, subtypes and brands. */
export async function catalogNames(backend: BackendClient): Promise<Map<string, string>> {
  const kinds = ['materials', 'subtypes', 'brands'] as const
  const rows = await Promise.all(
    kinds.map((kind) =>
      ok(backend.GET('/api/v1/filament/catalog/{kind}', { params: { path: { kind }, query: { include_archived: true } } }), `list the ${kind} catalog`),
    ),
  )
  return new Map(rows.flat().map((row) => [(row as { id: string }).id, (row as { name: string }).name]))
}

/** Settles to `fallback` when the read fails: a search still runs on the other fields. */
export const orElse = <T>(pending: Promise<T>, fallback: T): Promise<T> => pending.catch(() => fallback)
