import { describe, expect, it } from 'vitest'
import type { FilamentInventoryView, Spool, SpoolType } from '../api/filament'
import { addSku, matchesSearch, namesNumber, parseSearch, spoolSearchDoc, traysBySpool, typeSearchDoc } from './filamentSearch'

const spool = (values: Partial<Spool>) =>
  ({
    id: 's87',
    number: 87,
    colour_name: null,
    rgba: null,
    names: { brand: 'Bambu Lab', material: 'PLA', subtype: 'Basic', location: 'Shelf A' },
    effective: { colour_name: 'Hot Pink', rgba: '#F5547CFF' },
    ...values,
  }) as Spool

const find = (query: string, doc = spoolSearchDoc(spool({}), { typeCode: 'BL-PLA-BASIC-HOT-PINK', skus: ['A00-R3'], tray: 'H2C AMS-A slot 2' })) =>
  matchesSearch(doc, parseSearch(query))

describe('the filament search (#6894)', () => {
  it('finds a spool by its number, with or without #', () => {
    expect(find('#87')).toBe(true)
    expect(find('87')).toBe(true)
    expect(find('#8')).toBe(false)
    expect(find('#187')).toBe(false)
  })

  it('takes #87 as a number only, never a hex or a text', () => {
    const doc = spoolSearchDoc(spool({ number: 3, effective: { colour_name: 'Grey', rgba: '#878787FF' }, names: { location: 'Bin 87' } }))
    expect(matchesSearch(doc, parseSearch('#87'))).toBe(false)
    expect(matchesSearch(doc, parseSearch('87'))).toBe(true)
  })

  it('matches every field in any case: brand, material, subtype, colour, place, type code, SKU and tray', () => {
    for (const query of ['bambu', 'pla', 'BASIC', 'hot pink', 'shelf', 'bl-pla-basic', 'a00-r3', 'ams-a', 'h2c slot'])
      expect(find(query), query).toBe(true)
  })

  it('needs every token to match some field', () => {
    expect(find('pink pla')).toBe(true)
    expect(find('pink petg')).toBe(false)
  })

  it('matches a hex with or without #, from 3 characters', () => {
    expect(find('#F5547C')).toBe(true)
    expect(find('f5547c')).toBe(true)
    expect(find('5547cff')).toBe(true)
    expect(find('#00FF00')).toBe(false)
    // Two characters are too few to read as a colour.
    expect(find('f5')).toBe(false)
  })

  it('reads the spool own colour when it has no effective one', () => {
    const doc = spoolSearchDoc(spool({ colour_name: 'Teal', rgba: '#1B9AAAFF', effective: {} }))
    expect(matchesSearch(doc, parseSearch('teal 1b9aaa'))).toBe(true)
  })

  it('matches everything for an empty query', () => {
    expect(parseSearch('  ')).toEqual([])
    expect(parseSearch('#')).toEqual([])
    expect(find('')).toBe(true)
  })

  it('searches a type by code, catalog names, colour, hex and SKU', () => {
    const type = { id: 't', code: 'EL-PLA-SILK-GOLD', brand_id: 'b', material_id: 'm', subtype_id: 's', colour_name: 'Gold', rgba: '#D4AF37FF' } as SpoolType
    const names = new Map([
      ['b', 'Elegoo'],
      ['m', 'PLA'],
      ['s', 'Silk'],
    ])
    const doc = typeSearchDoc(type, names, ['ELG-SILK-GLD'])
    for (const query of ['elegoo silk', 'gold', '#d4af37', 'elg-silk', 'el-pla']) expect(matchesSearch(doc, parseSearch(query)), query).toBe(true)
    expect(matchesSearch(doc, parseSearch('#1'))).toBe(false)
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
