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
  const schema = {
    parameters: [
      { name: 'piece', type: 'select', group: 'Piece', initial: 'wall_window', caption: '' },
      { name: 'course', type: 'select', group: 'Piece', initial: 'upper', caption: '' },
      { name: 'module_size', type: 'number', group: 'Grid', initial: 150, caption: '' },
      { name: 'width_units', type: 'number', group: 'Grid', initial: 1, caption: '' },
      { name: 'exterior', type: 'select', group: 'Exterior', initial: 'plain', caption: '' },
    ],
  } as unknown as CustomizerSchema

  /** A host whose `set` merges one level deep and tells its subscribers, as the page does. */
  function fakeHost(initial: JsonObject) {
    let inputs = initial
    const listeners = new Set<(inputs: JsonObject) => void>()
    const set = vi.fn((patch: JsonObject) => {
      const next: JsonObject = { ...inputs }
      for (const [key, value] of Object.entries(patch)) {
        const old = next[key]
        next[key] =
          value && typeof value === 'object' && !Array.isArray(value) && old && typeof old === 'object' && !Array.isArray(old)
            ? { ...old, ...value }
            : value
      }
      inputs = next
      for (const listener of listeners) listener(inputs)
    })
    const host = {
      api: 1,
      inputs: {
        get: () => inputs,
        set,
        subscribe: (fn: (inputs: JsonObject) => void) => {
          listeners.add(fn)
          return () => listeners.delete(fn)
        },
      },
      schema: async () => schema,
      describe: () => undefined,
    } as unknown as Host
    return { host, set, get: () => inputs }
  }

  async function mounted(initial: JsonObject) {
    const { mount } = (await uiModule('index.js')) as { mount: Mount }
    const fake = fakeHost(initial)
    const holder = document.createElement('div')
    document.body.append(holder)
    const root = holder.attachShadow({ mode: 'open' })
    await mount(root, fake.host, { slot: 'page', version: null, theme: 'light', api: 1 })
    const button = (entry: string) => root.querySelector(`button[data-entry="${entry}"]`) as HTMLButtonElement
    return { ...fake, root, button }
  }

  it('lists the pieces and shows the one picked', async () => {
    const { root, set, button } = await mounted({ params: {} })
    expect(root.querySelector('sb-preview')).not.toBeNull()
    expect(root.querySelector('sb-generate')).not.toBeNull()
    expect(root.querySelector('sb-param[name="exterior"]')).not.toBeNull()
    expect(root.querySelector('sb-param[name="piece"]')).toBeNull()
    // The Grid sizes are the user's; a piece's size in modules is the designer's.
    expect(root.querySelector('sb-param[name="module_size"]')).not.toBeNull()
    expect(root.querySelector('sb-param[name="width_units"]')).toBeNull()
    button('floor_tile').click()
    expect(set).toHaveBeenLastCalledWith({
      params: { piece: 'floor_tile', course: 'upper', width_units: 1, depth_units: 1 },
    })
  })

  it('never writes inputs just by opening, and reads a missing house as the default', async () => {
    const { set, root } = await mounted({ params: { piece: 'wall' } })
    expect(set).not.toHaveBeenCalled()
    // DEFAULT_HOUSE is 2 x 1, 1 storey: P = 6, so 5 lower walls beside the door.
    expect(root.textContent).toContain('5 × Wall, lower course')
  })

  it('addresses each course of a coursed piece by its entry', async () => {
    const { root, button, get } = await mounted({ params: {} })
    expect(root.querySelectorAll('button[data-piece="wall"]')).toHaveLength(2)
    button('wall:lower').click()
    expect(get()['params']).toMatchObject({ piece: 'wall', course: 'lower' })
    // A floor tile after a lower wall does not keep the wall's course.
    button('floor_tile').click()
    expect(get()['params']).toMatchObject({ piece: 'floor_tile', course: 'upper' })
  })

  it('keeps the button just pressed, and its focus, when the list redraws', async () => {
    const { button, root } = await mounted({ params: {} })
    const floor = button('floor_tile')
    floor.focus()
    floor.click()
    expect(button('floor_tile')).toBe(floor)
    expect(root.contains(floor)).toBe(true)
    expect(root.activeElement).toBe(floor)
    expect(floor.textContent).toBe('Showing')
    expect(floor.getAttribute('aria-pressed')).toBe('true')
  })

  it('names each Show button after its piece', async () => {
    const { button } = await mounted({ params: {} })
    const floor = button('floor_tile')
    expect(floor.getAttribute('aria-label')).toBe('Show Floor tile')
    floor.click()
    expect(button('floor_tile').getAttribute('aria-label')).toBe('Showing Floor tile')
  })
})
