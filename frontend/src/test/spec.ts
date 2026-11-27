import { readFileSync } from 'node:fs'

/** The spec `pnpm gen:api` (run before `pnpm test`) exported, parsed, and where it was read
 * from. Tests read the backend's bounds and sets from it rather than restate them. */
export function readSpec(): { json: unknown; path: string } {
  const path = process.env.SCADBUDDY_OPENAPI_JSON ?? `${import.meta.dirname}/../../../backend/openapi.json`
  let text: string
  try {
    text = readFileSync(path, 'utf8')
  } catch (cause) {
    throw new Error(`no API spec at ${path}: run \`pnpm gen:api\` first (pnpm test does)`, { cause })
  }
  return { json: JSON.parse(text), path }
}

/** One keyword (`default`, `maxLength` and so on) of a property of a component schema in the
 * exported spec, checked to be of the type `kind` names. Throws, naming the spec's path, when
 * the schema, the property or the keyword is not there, or is of another type, so a renamed
 * or retyped backend field fails the test that reads it (#8134, #8152). */
export function schemaProperty<K extends keyof Kinds>(schema: string, property: string, keyword: string, kind: K): Kinds[K] {
  const { json, path } = readSpec()
  const spec = json as { components?: { schemas?: Record<string, { properties?: Record<string, Record<string, unknown>> }> } }
  const found = spec.components?.schemas?.[schema]?.properties?.[property]
  if (found === undefined) throw new Error(`${schema}.${property} not in the spec at ${path}`)
  const value = found[keyword]
  if (typeof value !== kind) {
    throw new Error(`${schema}.${property}.${keyword} is ${JSON.stringify(value)}, not a ${kind}, in the spec at ${path}`)
  }
  return value as Kinds[K]
}

interface Kinds {
  string: string
  number: number
}
