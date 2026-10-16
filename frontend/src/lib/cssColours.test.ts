import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { CSS_COLOURS } from './cssColours'

// The colour field converts a name with this table; the renderer reads names with the
// backend's. A name one knows and the other does not would render differently from
// what the field shows (#2357).
describe('CSS_COLOURS', () => {
  it("is the backend's table, entry for entry", () => {
    // Relative to this file, so the test reads the same file from any working directory.
    const source = readFileSync(path.resolve(import.meta.dirname, '../../../backend/scadbuddy/render/colours.py'), 'utf8')
    const backend = Object.fromEntries(
      [...source.matchAll(/^\s+"([a-z]+)": "(#[0-9A-F]{6})",$/gm)].map((m) => [m[1], m[2]]),
    )
    // Every entry line of the dict is one the pattern read: a line written otherwise (a
    // trailing comment, single quotes) would otherwise drop out of the comparison unseen.
    // Every line that is not blank or a comment counts, whatever it starts with.
    const dict = /^CSS_COLOURS: dict\[str, str\] = \{\n([\s\S]*?)^\}/m.exec(source)?.[1] ?? ''
    const entries = dict.split('\n').filter((line) => line.trim() !== '' && !line.trim().startsWith('#'))
    expect(entries.length).toBe(Object.keys(backend).length)
    expect(Object.keys(backend).length).toBeGreaterThan(140)
    expect(CSS_COLOURS).toEqual(backend)
  })
})
