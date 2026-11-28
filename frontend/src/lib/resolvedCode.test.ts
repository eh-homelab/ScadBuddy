import { describe, expect, it } from 'vitest'
import type { Resolved } from '../api/filament'
import { filamentLinks } from './filamentLinks'
import { inventoryPaths } from './inventoryLinks'
import { codeLabel, codeTarget } from './resolvedCode'

const resolved = (kind: Resolved['kind'], id: string, code: string): Resolved => ({
  kind,
  id,
  code,
  name: null,
  archived: false,
})

describe('resolved codes (#7169)', () => {
  it('names a type and an item', () => {
    expect(codeLabel(resolved('spool_type', 'a', 'T-4'))).toBe('Type T-4')
    expect(codeLabel(resolved('item', 'b', 'I-9'))).toBe('Item I-9')
  })

  it('opens the type page and the item page', () => {
    expect(codeTarget(resolved('spool_type', 'a', 'T-4'))).toBe(filamentLinks.type('a'))
    expect(codeTarget(resolved('item', 'b', 'I-9'))).toBe(inventoryPaths.item('b'))
  })
})
