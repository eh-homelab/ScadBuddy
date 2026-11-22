import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import type { FilamentInventoryView, Spool, SpoolType } from '../api/filament'
import { addSku, matchesSearch, namesNumber, parseSearch, spoolSearchDoc, traysBySpool, typeSearchDoc } from './filamentSearch'
import type { SpoolExtras } from './filamentSearch'

type Cases = { query: string; match: boolean }[]
type Vector =
  | { name: string; kind: 'spool'; spool: Spool; extras: SpoolExtras; cases: Cases }
  | { name: string; kind: 'type'; type: SpoolType; names: Record<string, string>; skus: string[]; cases: Cases }

// The agent's copy of the matcher runs the same file (agent/test/filamentSearch.test.ts).
const { groups } = JSON.parse(readFileSync(`${import.meta.dirname}/../../../test-fixtures/filament/search-vectors.json`, 'utf8')) as { groups: Vector[] }

describe('the filament search (#6894)', () => {
  for (const group of groups) {
    it(`matches the shared vectors: ${group.name}`, () => {
      const doc = group.kind === 'spool' ? spoolSearchDoc(group.spool, group.extras) : typeSearchDoc(group.type, new Map(Object.entries(group.names)), group.skus)
      for (const { query, match } of group.cases) expect(matchesSearch(doc, parseSearch(query)), JSON.stringify(query)).toBe(match)
    })
  }

  it('reads no tokens from an empty query', () => {
    expect(parseSearch('  ')).toEqual([])
    expect(parseSearch('#')).toEqual([])
  })

  it('names a spool number only for one token that is it', () => {
    expect(namesNumber(parseSearch('#87'), 87)).toBe(true)
    expect(namesNumber(parseSearch('87'), 87)).toBe(true)
    expect(namesNumber(parseSearch('87 pla'), 87)).toBe(false)
  })

  it('names each spool tray by printer and label', () => {
    const view = { spools: [], trays: [{ printer_id: 'p', printer_name: 'H2C', trays: [{ ams_id: 0, tray_id: 0, label: 'AMS-A slot 1', spool_id: 's1' }, { ams_id: 0, tray_id: 1, label: 'AMS-A slot 2' }] }] } as unknown as FilamentInventoryView
    expect(traysBySpool(view)).toEqual(new Map([['s1', 'H2C AMS-A slot 1']]))
  })

  it('keeps each SKU once per type', () => {
    const skus = new Map<string, string[]>()
    addSku(skus, 't', 'A')
    addSku(skus, 't', 'A')
    addSku(skus, 't', 'B')
    expect(skus.get('t')).toEqual(['A', 'B'])
  })
})
