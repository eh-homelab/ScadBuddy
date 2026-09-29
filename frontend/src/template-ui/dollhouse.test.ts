import { describe, expect, it, vi } from 'vitest'
import type { CustomizerSchema } from '../api/types'
import type { JsonObject } from '../lib/inputs'
import type { Host, Mount } from './types'

// The template's own files, as vite resolves them (a `new URL(…, import.meta.url)` is
// rewritten to an http URL the test runner cannot import, and a plain `import()` of a
// .js file outside `src/` has no types for tsc).
const ui = import.meta.glob('../../../models/dollhouse-kit/ui/*.js')
const uiModule = async (name: string) => {
  const loader = ui[`../../../models/dollhouse-kit/ui/${name}`]
  if (!loader) throw new Error(`models/dollhouse-kit/ui/${name} is missing`)
  return loader()
}
type Entry = { id: string; piece: string; course: string | null; count: number }
const load = async () =>
  (await uiModule('pieces.js')) as {
    housePieces: (h: object) => Entry[]
    clampHouse: (h: object) => object
    pieceParams: (e: Entry) => object
  }

describe('housePieces', () => {
  it('counts a one-room, one-storey house', async () => {
    const { housePieces } = await load()
    const counts = Object.fromEntries(
      housePieces({ cols: 1, rows: 1, storeys: 1, windows: 2 }).map((e) => [e.id, e.count]),
    )
    expect(counts).toEqual({
      'wall_door_lower': 1, 'wall:lower': 3, 'wall_door_upper': 1, 'wall_window': 2, 'wall:upper': 1,
      'corner_post:lower': 4, 'corner_post:upper': 4, 'floor_tile': 1, 'roof_panel': 1, 'door_leaf_lower': 1, 'door_leaf_upper': 1,
      'connectors': 1,
    })
  })

  it('counts a two-by-one, two-storey house with stairs', async () => {
    const { housePieces } = await load()
    const counts = Object.fromEntries(
      housePieces({ cols: 2, rows: 1, storeys: 2, windows: 3 }).map((e) => [e.id, e.count]),
    )
    // P = 6. Ground: 5 lower walls + door; upper: door + 3 windows + 2 walls.
    // First floor: 6 lower walls; upper: 3 windows + 3 walls.
    expect(counts['wall:lower']).toBe(11)
    expect(counts['wall_window']).toBe(6)
    expect(counts['wall:upper']).toBe(5)
    expect(counts['floor_tile']).toBe(4)
    expect(counts['roof_panel']).toBe(2)
    expect(counts['corner_post:lower']).toBe(8)
    expect(counts['corner_post:upper']).toBe(8)
    expect(counts['stairs_lower']).toBe(1)
    expect(counts['railing']).toBe(1)
  })

  it('clamps out-of-range and non-numeric house values', async () => {
    const { clampHouse } = await load()
    expect(clampHouse({ cols: 99, rows: -1, storeys: 'x', windows: 2.7 })).toEqual({ cols: 4, rows: 1, storeys: 1, windows: 2 })
  })

  it('renders one piece on the one-module grid', async () => {
    const { pieceParams } = await load()
    expect(pieceParams({ id: 'wall:upper', piece: 'wall', course: 'upper', count: 1 })).toEqual({
      piece: 'wall', course: 'upper', width_units: 1, depth_units: 1,
    })
  })

  it('pins the course of a corner post and a window wall after a lower wall was shown', async () => {
    const { housePieces, pieceParams } = await load()
    const byId = Object.fromEntries(housePieces({ cols: 1, rows: 1, storeys: 1, windows: 2 }).map((e) => [e.id, e]))
    // host.inputs.set is a JSON merge patch: a key the next Show leaves out keeps its value.
    const show = (params: object, id: string) => ({ ...params, ...pieceParams(byId[id] as Entry) })
    const afterLower = show({}, 'wall:lower')
    expect(afterLower).toMatchObject({ piece: 'wall', course: 'lower' })
    expect(show(afterLower, 'corner_post:upper')).toMatchObject({ piece: 'corner_post', course: 'upper' })
    expect(show(afterLower, 'wall_window')).toMatchObject({ piece: 'wall_window', course: 'upper' })
    expect(show(show({}, 'wall:upper'), 'corner_post:lower')).toMatchObject({ piece: 'corner_post', course: 'lower' })
  })
})

describe('the designer', () => {
  it('lists the pieces and shows the one picked', async () => {
    const { mount } = (await uiModule('index.js')) as { mount: Mount }
    const schema = {
      parameters: [
        { name: 'piece', type: 'select', group: 'Piece', initial: 'wall_window', caption: '' },
        { name: 'course', type: 'select', group: 'Piece', initial: 'upper', caption: '' },
        { name: 'exterior', type: 'select', group: 'Exterior', initial: 'plain', caption: '' },
      ],
    } as unknown as CustomizerSchema
    let inputs: JsonObject = { params: {} }
    const set = vi.fn((patch: JsonObject) => {
      inputs = { ...inputs, ...patch }
    })
    const host = {
      api: 1,
      inputs: { get: () => inputs, set, subscribe: () => () => undefined },
      schema: async () => schema,
      describe: () => undefined,
    } as unknown as Host
    const root = document.createElement('div').attachShadow({ mode: 'open' })
    await mount(root, host, { slot: 'page', version: null, theme: 'light', api: 1 })
    expect(root.querySelector('sb-preview')).not.toBeNull()
    expect(root.querySelector('sb-generate')).not.toBeNull()
    expect(root.querySelector('sb-param[name="exterior"]')).not.toBeNull()
    expect(root.querySelector('sb-param[name="piece"]')).toBeNull()
    expect(set).toHaveBeenCalledWith({ house: { cols: 2, rows: 1, storeys: 1, windows: 2 } })
    const floor = root.querySelector('button[data-piece="floor_tile"]') as HTMLButtonElement
    floor.click()
    expect(set).toHaveBeenLastCalledWith({ params: { piece: 'floor_tile', width_units: 1, depth_units: 1 } })
  })
})
