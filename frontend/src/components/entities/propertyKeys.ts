// A kind's property schema as rows of keys (#7067): the item kinds' form and every other kind's.

/** A key's JSON Schema type as the key table names it. */
export const KEY_TYPES = { string: 'Text', number: 'Number', integer: 'Whole number', boolean: 'Yes/no', array: 'List' } as const
export type KeyType = keyof typeof KEY_TYPES
/** One key row; `rid` names the row for React, so a removed row never hands its inputs to the next. */
export type KeyRow = { rid: string; key: string; type: KeyType; description: string; was: string | null; required: boolean }

let rowCount = 0
const rowId = () => `key-row-${(rowCount += 1)}`

/**
 * The keywords whose value only holds for one type (a bound, a length, a list's items, a
 * value of the type). A key whose type changes drops them; the server would refuse an
 * `enum` of the old type, and a bound of no use on the new one only misleads.
 */
const TYPED_KEYWORDS = ['enum', 'const', 'default', 'examples', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf', 'minLength', 'maxLength', 'pattern', 'format', 'items', 'prefixItems', 'contains', 'minItems', 'maxItems', 'uniqueItems']

/** The first type of a key's `type` that is not `null`; text when it names none the table knows. */
function keyTypeOf(sub: Record<string, unknown> | undefined): KeyType {
  const named = ([] as unknown[]).concat(sub?.type ?? []).find((t) => t !== 'null')
  return typeof named === 'string' && named in KEY_TYPES ? (named as KeyType) : 'string'
}

const propertiesOf = (schema: Record<string, unknown>) => (schema.properties && typeof schema.properties === 'object' ? (schema.properties as Record<string, Record<string, unknown>>) : {})

/** The keys a schema requires (#7703). */
export const requiredOf = (schema: Record<string, unknown>): string[] => (Array.isArray(schema.required) ? schema.required.filter((k): k is string => typeof k === 'string') : [])

export function rowsOf(schema: Record<string, unknown>): KeyRow[] {
  const required = new Set(requiredOf(schema))
  return Object.entries(propertiesOf(schema)).map(([key, sub]) => ({ rid: rowId(), key, type: keyTypeOf(sub), description: typeof sub.description === 'string' ? sub.description : '', was: key, required: required.has(key) }))
}

/**
 * The kind's own schema with the table's keys. A key keeps every other keyword it had (an
 * `enum`, a bound) while its type stays; a type left as it was keeps its union with `null`.
 * A changed type drops the keywords of the old one (`TYPED_KEYWORDS`). The server checks the
 * whole schema (`bad_property_schema`), and a schema never refuses a key (spec §6.3a).
 */
export function schemaOf(own: Record<string, unknown>, rows: KeyRow[]): Record<string, unknown> {
  const before = propertiesOf(own)
  const properties: Record<string, unknown> = {}
  for (const row of rows) {
    const key = row.key.trim()
    if (!key) continue
    const old = row.was !== null ? before[row.was] : undefined
    const sub: Record<string, unknown> = { ...old }
    if (old && keyTypeOf(old) !== row.type) for (const keyword of TYPED_KEYWORDS) delete sub[keyword]
    if (!old || keyTypeOf(old) !== row.type) sub.type = row.type
    if (row.type === 'array') sub.items = (old?.items as unknown) ?? { type: 'string' }
    else delete sub.items
    if (row.description.trim()) sub.description = row.description.trim()
    else delete sub.description
    properties[key] = sub
  }
  const required = [...new Set(rows.filter((row) => row.required && row.key.trim()).map((row) => row.key.trim()))]
  const schema: Record<string, unknown> = { ...own, type: 'object', properties }
  if (required.length) schema.required = required
  else delete schema.required
  return schema
}

/** A row for a key not named yet. */
export const newRow = (): KeyRow => ({ rid: rowId(), key: '', type: 'string', description: '', was: null, required: false })

/** Whether two rows name the same key. */
export function namedTwice(rows: KeyRow[]): boolean {
  const keys = rows.map((row) => row.key.trim()).filter(Boolean)
  return new Set(keys).size !== keys.length
}
