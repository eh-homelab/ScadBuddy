import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { MAX_SOURCE_FILES, newSourceFileProblem, SOURCE_FILE_PATTERN, sourceFileName } from './sourceFiles'

describe('sourceFileName', () => {
  it('adds .scad when it is left off, and trims', () => {
    expect(sourceFileName(' parts ')).toBe('parts.scad')
    expect(sourceFileName('parts.scad')).toBe('parts.scad')
    expect(sourceFileName('  ')).toBe('')
  })
})

describe('newSourceFileProblem', () => {
  const existing = ['model.scad', 'parts.scad']

  it('accepts a new name the backend accepts', () => {
    expect(newSourceFileProblem('lid_v2.scad', existing)).toBeNull()
    expect(newSourceFileProblem(`${'a'.repeat(96)}.scad`, existing)).toBeNull()
  })

  it("refuses names the backend's SOURCE_FILE_PATTERN refuses", () => {
    for (const name of ['.hidden.scad', 'dir/parts.scad', 'has space.scad', `${'a'.repeat(97)}.scad`, 'parts.txt']) {
      expect(newSourceFileProblem(name, existing), name).toMatch(/letters, digits/)
    }
  })

  it('refuses model.scad, a name already there, and a model already full', () => {
    expect(newSourceFileProblem('model.scad', existing)).toMatch(/own source/)
    expect(newSourceFileProblem('parts.scad', existing)).toMatch(/already has parts.scad/)
    const full = Array.from({ length: MAX_SOURCE_FILES }, (_, i) => `f${i}.scad`)
    expect(newSourceFileProblem('more.scad', full)).toMatch(/at most 50/)
  })
})

describe('the rules mirrored from the backend (#2231)', () => {
  // Read as text: a change to either rule there fails here, not as a name the page
  // accepts and the route refuses.
  const backend = readFileSync(`${import.meta.dirname}/../../../backend/scadbuddy/api/model_files.py`, 'utf8')

  it('uses the same file-name pattern', () => {
    expect(backend.match(/^SOURCE_FILE_PATTERN = r"(.+)"$/m)?.[1]).toBe(SOURCE_FILE_PATTERN.source)
  })

  it('allows the same number of files', () => {
    expect(Number(backend.match(/^MAX_SOURCE_FILES = (\d+)$/m)?.[1])).toBe(MAX_SOURCE_FILES)
  })
})
