import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { OPENS, SHOWN_IMAGES, THUMBNAILED } from './attachmentMedia'

type Extension = 'x-inline' | 'x-shown-images' | 'x-thumbnailed'

/** One of the type sets the spec `pnpm gen:api` (run before `pnpm test`) exported carries
 * on `Attachment.media_type`: the backend's own (`orders/models.py`), read, not restated. */
function specTypes(extension: Extension): string[] {
  const path = process.env.SCADBUDDY_OPENAPI_JSON ?? `${import.meta.dirname}/../../../../backend/openapi.json`
  let text: string
  try {
    text = readFileSync(path, 'utf8')
  } catch (cause) {
    throw new Error(`no API spec at ${path}: run \`pnpm gen:api\` first (pnpm test does)`, { cause })
  }
  const spec = JSON.parse(text) as {
    components?: { schemas?: { Attachment?: { properties?: { media_type?: Partial<Record<Extension, unknown>> } } } }
  }
  const types = spec.components?.schemas?.Attachment?.properties?.media_type?.[extension]
  if (!Array.isArray(types)) throw new Error(`Attachment.media_type.${extension} not in the spec at ${path}`)
  return types as string[]
}

describe('attachment media types (#4679)', () => {
  it.each([
    ['x-inline', OPENS],
    ['x-shown-images', SHOWN_IMAGES],
    ['x-thumbnailed', THUMBNAILED],
  ] as const)('match the backend: %s', (extension, types) => {
    expect([...types].sort()).toEqual([...specTypes(extension)].sort())
  })
})
