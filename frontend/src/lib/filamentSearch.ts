import type { FilamentInventoryView, Spool, SpoolType, TagRow } from '../api/filament'
import { listProducts, listSuppliers } from '../api/filamentPurchasing'
import { entityApi, type PropertyValue } from '../components/entities/api'

/**
 * #6894 — the filament text search: the Spools and Types tabs' `q=`, the quick find, and
 * (mirrored in `agent/src/tools/filamentSearch.ts`) the agent's `filament_spool_list` and
 * `filament_type_list` `q`. The query is split on white space, and every token must match
 * some field, in any case:
 *
 * - `#87` (a `#` and 1 to 5 digits) is a spool number, and matches spool 87 only.
 * - Any other token matches a spool whose number it is (`87`), a text field that contains it
 *   (brand, material, colour name, SKU, place…), or, from 3 characters, a colour whose hex
 *   contains it, with or without its `#` (`#FF6A13`, `ff6a13`).
 * - #7151: from 4 hex digits (`:` and `-` left out), a tag UID or tray UUID that holds it (`04:A1:B2`).
 * - A spool's or type's property values (#7026) are text fields too: a GTIN, an MPN, a lot.
 */
export interface SearchToken {
  /** The token in lower case, without a leading `#`. */
  text: string
  /** `#87`: only a spool number matches. */
  numberOnly: boolean
}

/** What a spool or type is searched by. */
export interface SearchDoc {
  number?: number | null
  texts: readonly (string | null | undefined)[]
  /** `#RRGGBB` or `#RRGGBBAA` colours. */
  hexes: readonly (string | null | undefined)[]
  /** #7151 — the RFID keys of its open tag: the UID and the tray UUID, hex. */
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

/** Every token matches some field of `doc`; no token matches everything. */
export function matchesSearch(doc: SearchDoc, tokens: readonly SearchToken[]): boolean {
  return tokens.every((token) => matchesToken(doc, token))
}

/** Whether `tokens` name this spool number exactly (`#87` or `87`): the quick find lists it first. */
export function namesNumber(tokens: readonly SearchToken[], number: number): boolean {
  return tokens.length === 1 && isNumber(tokens[0]!.text, number)
}

/** What else a spool is searched by: its type's code and SKUs, the tray it is in, and its properties. */
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

/** An entity's property values as search text: a string as it is, each list item, a number or boolean as text. */
export function propertyTexts(values: Record<string, PropertyValue> | undefined): string[] {
  return Object.values(values ?? {}).flatMap((value) => (Array.isArray(value) ? value : [String(value)]))
}

/**
 * Every entity of `kind`'s property values as search text, by ref. A failed read gives an
 * empty map: the search still runs on everything else.
 */
export async function loadKindPropertyTexts(kind: 'spool' | 'spool_type'): Promise<Map<string, string[]>> {
  try {
    const found = await entityApi.kindProperties(kind)
    return new Map(Object.entries(found).map(([ref, values]) => [ref, propertyTexts(values)]))
  } catch {
    return new Map()
  }
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

/** A type, its catalog names read from `names` (catalog row id to name). */
export function typeSearchDoc(type: SpoolType, names: ReadonlyMap<string, string>, skus: readonly string[] = [], extras: TypeExtras = {}): SearchDoc {
  const name = (id: string | null | undefined) => (id ? names.get(id) : null)
  return {
    texts: [type.code, name(type.brand_id), name(type.material_id), name(type.subtype_id), type.colour_name, ...skus, ...(extras.properties ?? [])],
    hexes: [type.rgba],
  }
}

/** The tray each spool is in, "H2C AMS-A slot 1", from the printers' latest reports. */
export function traysBySpool(view: FilamentInventoryView): Map<string, string> {
  const found = new Map<string, string>()
  for (const printer of view.trays) {
    for (const tray of printer.trays) if (tray.spool_id) found.set(tray.spool_id, `${printer.printer_name} ${tray.label}`)
  }
  return found
}

/** #7151 — each spool's open tag keys, its UID and tray UUID, for the search by tag. */
export function tagsBySpool(tags: readonly TagRow[]): Map<string, string[]> {
  const found = new Map<string, string[]>()
  for (const tag of tags) {
    if (tag.binding) found.set(tag.binding.spool_id, [tag.tag_uid, tag.tray_uuid].filter((key): key is string => !!key))
  }
  return found
}

/** Adds `sku` to `type`'s list in `into`. */
export function addSku(into: Map<string, string[]>, typeId: string, sku: string) {
  const list = into.get(typeId)
  if (!list) into.set(typeId, [sku])
  else if (!list.includes(sku)) list.push(sku)
}

/**
 * Each type's SKUs, from the products of every supplier not archived. A supplier whose
 * products cannot be read leaves its SKUs out: the search still runs on everything else.
 */
export async function loadTypeSkus(): Promise<Map<string, string[]>> {
  const skus = new Map<string, string[]>()
  const suppliers = (await listSuppliers()).filter((supplier) => !supplier.archived_at)
  const lists = await Promise.allSettled(suppliers.map((supplier) => listProducts(supplier.id)))
  for (const list of lists) {
    if (list.status === 'fulfilled') for (const product of list.value) addSku(skus, product.type_id, product.sku)
  }
  return skus
}
