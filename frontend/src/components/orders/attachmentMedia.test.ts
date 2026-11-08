import { describe, expect, it } from 'vitest'
import { OPENS, SHOWN_IMAGES, THUMBNAILED } from './attachmentMedia'
import { readSpec } from '../../test/spec'

type Extension = 'x-inline' | 'x-shown-images' | 'x-thumbnailed'

/** One of the type sets the spec `pnpm gen:api` (run before `pnpm test`) exported carries
 * on `Attachment.media_type`: the backend's own (`orders/models.py`), read, not restated. */
function specTypes(extension: Extension): string[] {
  const { json, path } = readSpec()
  const spec = json as {
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
