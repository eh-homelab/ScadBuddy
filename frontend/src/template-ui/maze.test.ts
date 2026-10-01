import { describe, expect, it } from 'vitest'
import type { CustomizerSchema } from '../api/types'
import type { JsonObject } from '../lib/inputs'
import type { Host, Mount } from './types'

const SCHEMA = {
  parameters: [
    { name: 'mode', type: 'select', group: 'Play', initial: 'open_tray', caption: '', options: [] },
    { name: 'ball_d', type: 'slider', group: 'Play', initial: 6, caption: '' },
    { name: 'wall_color', type: 'color', group: 'Colours', initial: '#006064', caption: '' },
    { name: 'lid_color', type: 'color', group: 'Colours', initial: '#FFFFFF', caption: '' },
  ],
} as unknown as CustomizerSchema

// The template's own module, as vite resolves it (a `new URL(…, import.meta.url)` is
// rewritten to an http URL the test runner cannot import, and a plain `import()` of a
// .js file outside `src/` has no types for tsc).
const ui = import.meta.glob('../../../models/maze-puzzle/ui/*.js')

function fakeHost(initial: JsonObject) {
  let inputs = initial
  const listeners: ((i: JsonObject) => void)[] = []
  let describe: (() => string) | null = null
  const host = {
    api: 1,
    inputs: {
      get: () => inputs,
      set: () => undefined,
      subscribe: (fn: (i: JsonObject) => void) => {
        listeners.push(fn)
        return () => undefined
      },
    },
    schema: async () => SCHEMA,
    describe: (fn: () => string) => {
      describe = fn
    },
  } as unknown as Host
  return {
    host,
    describe: () => describe?.() ?? null,
    change(next: JsonObject) {
      inputs = next
      for (const fn of listeners) fn(next)
    },
  }
}

async function mountMaze(initial: JsonObject) {
  const loader = ui['../../../models/maze-puzzle/ui/index.js']
  if (!loader) throw new Error('models/maze-puzzle/ui/index.js is missing')
  const { mount } = (await loader()) as { mount: Mount }
  const root = document.createElement('div').attachShadow({ mode: 'open' })
  const fake = fakeHost(initial)
  const cleanup = await mount(root, fake.host, { slot: 'panel', version: null, theme: 'light', api: 1 })
  const lid = () => root.querySelector('sb-param[name="lid_color"]') as HTMLElement
  return { root, lid, change: fake.change, describe: fake.describe, cleanup }
}

describe('maze-puzzle ui', () => {
  it('renders every parameter, grouped, and hides the lid colour without a lid', async () => {
    const { root, lid } = await mountMaze({ params: { mode: 'open_tray' } })
    expect(Array.from(root.querySelectorAll('sb-param')).map((el) => el.getAttribute('name'))).toEqual([
      'mode', 'ball_d', 'wall_color', 'lid_color',
    ])
    expect(Array.from(root.querySelectorAll('h3')).map((el) => el.textContent)).toEqual(['Play', 'Colours'])
    expect(lid().hidden).toBe(true)
  })

  it('shows it once the mode has a lid, and hides it again', async () => {
    const { lid, change } = await mountMaze({ params: { mode: 'open_tray' } })
    change({ params: { mode: 'ball_lid' } })
    expect(lid().hidden).toBe(false)
    change({ params: { mode: 'open_tray' } })
    expect(lid().hidden).toBe(true)
  })

  it('describes the model in the mode it is in, for the assistant', async () => {
    const { change, describe } = await mountMaze({ params: { mode: 'open_tray' } })
    expect(describe()).toBe('Open-tray ball maze; lid_color is hidden because there is no lid.')
    change({ params: { mode: 'ball_lid' } })
    expect(describe()).toBe('Ball maze with a snap-on lid; lid_color is shown.')
  })

  it('reads the default mode when the inputs leave it out', async () => {
    const { lid } = await mountMaze({ params: {} })
    expect(lid().hidden).toBe(true)
  })

  it('removes what it drew on cleanup', async () => {
    const { root, cleanup } = await mountMaze({ params: {} })
    if (typeof cleanup === 'function') cleanup()
    expect(root.childNodes.length).toBe(0)
  })
})
