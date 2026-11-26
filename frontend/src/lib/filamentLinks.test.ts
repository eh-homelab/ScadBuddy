import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { filamentLinks } from './filamentLinks'

interface Vector {
  kind: 'spool' | 'type' | 'supplier' | 'invoice' | 'location' | 'catalog'
  key: string
  path: string
}

// The agent's copy (`agent/src/tools/filamentLinks.ts`) is tested against the same vectors.
const vectors = JSON.parse(readFileSync(`${import.meta.dirname}/../../../test-fixtures/filament/ui-paths.json`, 'utf8')) as Vector[]

describe('filamentLinks (#6869)', () => {
  it.each(vectors)('$kind $key is $path', ({ kind, key, path }) => {
    const build: Record<Vector['kind'], (key: string) => string> = {
      spool: filamentLinks.spool,
      type: filamentLinks.type,
      supplier: filamentLinks.supplier,
      invoice: filamentLinks.invoice,
      location: filamentLinks.location,
      catalog: (kind) => filamentLinks.catalog(kind as Parameters<typeof filamentLinks.catalog>[0]),
    }
    expect(build[kind](key)).toBe(path)
  })

  it('anchors an invoice line and a supplier product on their pages', () => {
    expect(filamentLinks.invoiceLine('i1', 'l 2')).toBe('/filament/invoices/i1#line-l%202')
    expect(filamentLinks.supplierProduct('s1', 'p1')).toBe('/filament/suppliers/s1#product-p1')
  })

  it('filters the lists that stand in for a page', () => {
    expect(filamentLinks.supplierInvoices('s1')).toBe('/filament/invoices?supplier=s1')
    expect(filamentLinks.labels(['a', 'b'])).toBe('/filament/labels?ids=a,b')
    expect(filamentLinks.locationSpools('a b')).toBe('/filament/spools?place=a+b')
    expect(filamentLinks.printerLabels('p/1')).toBe('/filament/labels?printers=p%2F1')
    expect(filamentLinks.spools).toBe('/filament/spools')
    expect(filamentLinks.newSpool).toBe('/filament/spools/new')
    expect(filamentLinks.scan).toBe('/scan')
    expect(filamentLinks.copySpool('a b')).toBe('/filament/spools/new?copy=a+b')
    expect(filamentLinks.newType).toBe('/filament/types/new')
    expect(filamentLinks.printers).toBe('/settings/printing#printers')
  })

  it('names each list', () => {
    expect(filamentLinks.types).toBe('/filament/types')
    expect(filamentLinks.invoices).toBe('/filament/invoices')
    expect(filamentLinks.suppliers).toBe('/filament/suppliers')
    expect(filamentLinks.locations).toBe('/filament/locations')
    expect(filamentLinks.shoppingList).toBe('/filament/shopping-list')
  })
})
