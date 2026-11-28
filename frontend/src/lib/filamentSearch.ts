import type { FilamentInventoryView, Spool, SpoolType, TagRow } from '../api/filament'
import { listProducts, listSuppliers } from '../api/filamentPurchasing'

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
 * - A spool's or type's property values (#7026) are text fields too (a GTIN, an MPN, a lot),
 *   here and in the agent's copy; a key's name is not. A spool's search reads its type's
 *   property values too (#8372).
 * - #8372: a `key:value` token (`gtin:06975337041234`, lower case, key `[a-z][a-z0-9_]{0,63}`)
 *   also matches a property with that key whose whole value, or one item of a list, equals the
 *   value in any case. It is not a substring match. Two values of 8 to 14 digits are equal when
 *   they are equal padded to 14 digits (the GTIN-14 form), so an EAN-13 equals its stored GTIN-14.
 *   A `key:value` token still matches by every plain rule on its whole text, so a tag UID
 *   such as `a1:b2:c3:d4` is found as before.
 */
export interface SearchToken {
  /** The token in lower case, without a leading `#`. */
  text: string
  /** `#87`: only a spool number matches. */
  numberOnly: boolean
  /** #8372: a `key:value` token: the property key and the value to equal. */
  property?: { key: string; value: string }
}

/** A property value as search text, with its key. */
export interface PropertyText {
  key: string
  text: string
}

/** What a spool or type is searched by. */
export interface SearchDoc {
  number?: number | null
  texts: readonly (string | null | undefined)[]
  /** `#RRGGBB` or `#RRGGBBAA` colours. */
  hexes: readonly (string | null | undefined)[]
  /** #7151 — the RFID keys of its open tag: the UID and the tray UUID, hex. */
  tags?: readonly string[]
  /** #7026, #8372 — its property values as text, each with its key. */
  properties?: readonly PropertyText[]
}

export function parseSearch(query: string | null | undefined): SearchToken[] {
  return (query ?? '')
    .toLowerCase()
    .split(/\s+/)
    .filter((raw) => raw !== '' && raw !== '#')
    .map((raw) => {
      const text = raw.replace(/^#/, '')
      const found = /^([a-z][a-z0-9_]{0,63}):(.+)$/.exec(text)
      return { text, numberOnly: /^#\d{1,5}$/.test(raw), ...(found && { property: { key: found[1]!, value: found[2]! } }) }
    })
}

const isNumber = (text: string, number: number | null | undefined) => number != null && /^\d+$/.test(text) && Number(text) === number

const isGtinLength = (text: string) => /^\d{8,14}$/.test(text)

/** A stored value equals the token's: in any case, or as GTIN-14 when both are 8 to 14 digits. */
function equalsValue(text: string, value: string): boolean {
  const stored = text.trim().toLowerCase()
  return stored === value || (isGtinLength(stored) && isGtinLength(value) && stored.padStart(14, '0') === value.padStart(14, '0'))
}

function matchesToken(doc: SearchDoc, token: SearchToken): boolean {
  if (isNumber(token.text, doc.number)) return true
  if (token.numberOnly) return false
  if (doc.texts.some((text) => text?.toLowerCase().includes(token.text))) return true
  const properties = doc.properties ?? []
  if (properties.some((property) => property.text.toLowerCase().includes(token.text))) return true
  const wanted = token.property
  if (wanted && properties.some((property) => property.key === wanted.key && equalsValue(property.text, wanted.value))) return true
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
  properties?: readonly PropertyText[]
  /** #8372 — its type's property values as text (`propertyTexts`). */
  typeProperties?: readonly PropertyText[]
}

/** What else a type is searched by: its property values as text (`propertyTexts`). */
export interface TypeExtras {
  properties?: readonly PropertyText[]
}

/**
 * An entity's property values as search text, each with its key: a string as it is, each list item,
 * a number as text. A boolean is left out: "true" would match every entity with any yes/no property.
 */
export function propertyTexts(values: Readonly<Record<string, string | number | boolean | readonly string[]>> | undefined): PropertyText[] {
  return Object.entries(values ?? {}).flatMap(([key, value]) =>
    (Array.isArray(value) ? value : typeof value === 'boolean' ? [] : [String(value)]).map((text: string) => ({ key, text })),
  )
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
    ],
    hexes: [spool.effective?.rgba ?? spool.rgba],
    tags: extras.tags ?? [],
    properties: [...(extras.properties ?? []), ...(extras.typeProperties ?? [])],
  }
}

/** A type, its catalog names read from `names` (catalog row id to name). */
export function typeSearchDoc(type: SpoolType, names: ReadonlyMap<string, string>, skus: readonly string[] = [], extras: TypeExtras = {}): SearchDoc {
  const name = (id: string | null | undefined) => (id ? names.get(id) : null)
  return {
    texts: [type.code, name(type.brand_id), name(type.material_id), name(type.subtype_id), type.colour_name, ...skus],
    hexes: [type.rgba],
    properties: extras.properties ?? [],
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
