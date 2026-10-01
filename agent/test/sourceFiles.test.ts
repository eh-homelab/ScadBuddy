import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { MAX_MESSAGE_CHARS, MAX_SOURCE_CHARS, sourceFileTools } from '../src/tools/sourceFiles.js'

// The agent refuses a file the backend would take, or passes one it would refuse,
// if the two bounds drift (PR #752 review). The spec is the one `pnpm gen:api`
// exported, as in coverage.test.ts.
type Property = { maxLength?: number; anyOf?: { maxLength?: number }[] }
const spec = JSON.parse(readFileSync(new URL('../../backend/openapi.json', import.meta.url), 'utf8')) as {
  components: { schemas: Record<string, { properties: Record<string, Property> }> }
}
const update = spec.components.schemas.SourceFileUpdate?.properties
const write = sourceFileTools.find((tool) => tool.name === 'write_source_file')!
const args = (content: string) => ({ slug: 'keychain', name: 'parts.scad', content })

describe('write_source_file', () => {
  it("bounds content and message as the backend's SourceFileUpdate does", () => {
    expect(update?.content?.maxLength).toBe(MAX_SOURCE_CHARS)
    expect(update?.message?.anyOf?.find((option) => option.maxLength)?.maxLength).toBe(MAX_MESSAGE_CHARS)
  })

  it('counts content in code points, as Pydantic does', () => {
    // Each emoji is two UTF-16 units, one code point: past a `.max` on `length`,
    // within the backend's bound.
    expect(() => write.parse(args('\u{1F600}'.repeat(MAX_SOURCE_CHARS / 2 + 1)))).not.toThrow()
    expect(() => write.parse(args('x'.repeat(MAX_SOURCE_CHARS + 1)))).toThrow(/at most/)
    const message = (text: string) => ({ ...args('x = 1;'), message: text })
    expect(() => write.parse(message('\u{1F600}'.repeat(MAX_MESSAGE_CHARS)))).not.toThrow()
    expect(() => write.parse(message('m'.repeat(MAX_MESSAGE_CHARS + 1)))).toThrow(/at most/)
  })
})
