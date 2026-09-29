import { describe, expect, it } from 'vitest'

// `node:fs` through a dynamic import: this file is type-checked with the app, which has
// no Node types, and vite refuses `?raw` imports from outside frontend/.
async function read(path: string): Promise<Uint8Array> {
  const specifier: string = 'node:fs'
  const fs = (await import(/* @vite-ignore */ specifier)) as { readFileSync(path: URL): Uint8Array }
  return fs.readFileSync(new URL(path, import.meta.url))
}

describe('mock copies of bundled template UIs', () => {
  it('maze-puzzle.js is byte-identical to models/maze-puzzle/ui/index.js', async () => {
    const copy = await read('./maze-puzzle.js')
    const original = await read('../../../../models/maze-puzzle/ui/index.js')
    expect(copy.length).toBeGreaterThan(0)
    expect(Array.from(copy)).toEqual(Array.from(original))
  })
})
