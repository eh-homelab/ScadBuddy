import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { MAX_SOURCE_CHARS } from '../src/tools/sourceFiles.js'

// The agent refuses a file the backend would take, or passes one it would refuse,
// if the two bounds drift (PR #752 review). The spec is the one `pnpm gen:api`
// exported, as in coverage.test.ts.
const spec = JSON.parse(readFileSync(new URL('../../backend/openapi.json', import.meta.url), 'utf8')) as {
  components: { schemas: Record<string, { properties: Record<string, { maxLength?: number }> }> }
}

describe('write_source_file', () => {
  it("bounds content as the backend's SourceFileUpdate does", () => {
    expect(spec.components.schemas.SourceFileUpdate?.properties.content?.maxLength).toBe(MAX_SOURCE_CHARS)
  })
})
