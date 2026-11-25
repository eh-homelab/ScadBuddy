import { readFileSync } from 'node:fs'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { inventoryPaths, withInventoryPaths } from '../src/tools/inventoryLinks.js'
import type { Tool, ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #7067: the inventory pages the agent links, against the vectors the frontend's
// `lib/inventoryLinks.ts` is tested with.

interface Vector {
  kind: 'item' | 'unit' | 'lot' | 'kind' | 'offer'
  keys: string[]
  path: string
}
const vectors = JSON.parse(readFileSync(`${import.meta.dirname}/../../test-fixtures/inventory/ui-paths.json`, 'utf8')) as Vector[]

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('write') },
    progress: async () => {},
    signal: new AbortController().signal,
  }
}

const call = (kind: Vector['kind'], keys: string[]): string => {
  switch (kind) {
    case 'offer':
      return inventoryPaths.offer(keys[0]!, keys[1]!)
    default:
      return inventoryPaths[kind](keys[0]!)
  }
}

describe('inventory ui paths (#7067)', () => {
  it('reads every vector', () => {
    expect(new Set(vectors.map((v) => v.kind))).toEqual(new Set(['item', 'unit', 'lot', 'kind', 'offer']))
  })

  it.each(vectors)("$kind $keys is the frontend's $path", ({ kind, keys, path }) => {
    expect(call(kind, keys)).toBe(path)
  })

  it('gives an entity tool on an item, a unit and a lot its page', async () => {
    for (const vector of vectors.filter((v) => v.kind === 'item' || v.kind === 'unit' || v.kind === 'lot')) {
      const ref = `${vector.kind}:${vector.keys[0]!}`
      server.use(http.get(`${BACKEND}/api/v1/entities/${encodeURIComponent(ref)}/links`, () => HttpResponse.json({ links: [] })))
      expect(firstText(await tool('entity_link_list').execute({ entity: ref }, ctx())), ref).toEqual({ links: [], entity_ui_path: vector.path })
    }
  })

  it('leaves a row with no id, and a move, without a page of its own', () => {
    expect(withInventoryPaths({ name: 'x' }, 'item')).toEqual({ name: 'x' })
    expect(withInventoryPaths({ id: 'm1', item_id: 'i1' }, 'move')).toEqual({ id: 'm1', item_id: 'i1', item_ui_path: '/inventory/items/i1' })
    expect(withInventoryPaths({ id: 'o1' }, 'offer')).toEqual({ id: 'o1' })
  })
})
