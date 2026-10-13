import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { CSS_COLOURS } from './cssColours'

// The colour field converts a name with this table; the renderer reads names with the
// backend's. A name one knows and the other does not would render differently from
// what the field shows (#2357).
describe('CSS_COLOURS', () => {
  it("is the backend's table, entry for entry", () => {
    // Vitest runs from frontend/.
    const source = readFileSync(path.resolve(process.cwd(), '../backend/scadbuddy/render/colours.py'), 'utf8')
    const backend = Object.fromEntries(
      [...source.matchAll(/^\s+"([a-z]+)": "(#[0-9A-F]{6})",$/gm)].map((m) => [m[1], m[2]]),
    )
    expect(Object.keys(backend).length).toBeGreaterThan(140)
    expect(CSS_COLOURS).toEqual(backend)
  })
})
