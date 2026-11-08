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
