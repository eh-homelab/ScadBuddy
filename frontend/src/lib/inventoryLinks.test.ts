import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { inventoryAnchors, inventoryPaths } from './inventoryLinks'

interface Vector {
  kind: 'item' | 'unit' | 'lot' | 'kind' | 'offer'
  keys: string[]
  path: string
}

// The agent's copy (layer 5, Task 12) is tested against the same vectors.
const vectors = JSON.parse(readFileSync(`${import.meta.dirname}/../../../test-fixtures/inventory/ui-paths.json`, 'utf8')) as Vector[]

describe('inventoryPaths (#7067)', () => {
  it.each(vectors)('$kind $keys is $path', ({ kind, keys, path }) => {
    const build: Record<Vector['kind'], (keys: string[]) => string> = {
      item: ([id]) => inventoryPaths.item(id!),
      unit: ([id]) => inventoryPaths.unit(id!),
      lot: ([id]) => inventoryPaths.lot(id!),
      kind: ([id]) => inventoryPaths.kind(id!),
      offer: ([item, offer]) => inventoryPaths.offer(item!, offer!),
    }
    expect(build[kind](keys)).toBe(path)
  })

  it('names the lists and anchors a lot on its item page', () => {
    expect(inventoryPaths.list).toBe('/inventory')
    expect(inventoryPaths.kinds).toBe('/inventory/kinds')
    expect(inventoryPaths.itemLot('i 1', 'l1')).toBe('/inventory/items/i%201#lot-l1')
    expect(inventoryAnchors.lot('l1')).toBe('lot-l1')
    expect(inventoryAnchors.offer('o1')).toBe('offer-o1')
  })
})
