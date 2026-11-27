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

/** One property of a component schema in the exported spec, as the spec writes it (its
 * `default`, `maxLength` and so on). Throws, naming the spec's path, when the schema or the
 * property is not there, so a renamed backend field fails the test that reads it (#8134). */
export function schemaProperty(schema: string, property: string): Record<string, unknown> {
  const { json, path } = readSpec()
  const spec = json as { components?: { schemas?: Record<string, { properties?: Record<string, Record<string, unknown>> }> } }
  const found = spec.components?.schemas?.[schema]?.properties?.[property]
  if (found === undefined) throw new Error(`${schema}.${property} not in the spec at ${path}`)
  return found
}
