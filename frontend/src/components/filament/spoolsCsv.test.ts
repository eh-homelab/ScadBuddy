import { describe, expect, it } from 'vitest'
import type { Spool } from '../../api/filament'
import { DERIVED_CSV_COLUMNS, EXPORT_COLUMNS, parseCsv, readSpoolsCsv, SPOOL_CSV_COLUMNS, spoolsCsv } from './spoolsCsv'

const spool = {
  id: 's1',
  number: 12,
  type_id: 't1',
  colour_name: null,
  note: 'Bought at "Micro Center", aisle 4',
  weight_locked: false,
  grams_left: 640,
  last_weighed_g: 640,
  last_weighed_at: '2026-11-14T18:00:00Z',
  last_dried_at: null,
  last_used_at: null,
  archived_at: null,
  low_stock: false,
  effective: {
    colour_name: 'Red, Matte',
    rgba: '#FF000080',
    extra_colours: ['#00FF00FF', '#0000FFFF'],
    label_weight_g: 1000,
    diameter_mm: '1.75',
  },
  names: { material: 'PLA', brand: 'Bambu Lab', subtype: null, category: null, effect: null, insert_type: null, location: 'Shelf A' },
} as unknown as Spool

describe('the spools CSV', () => {
  it('quotes a cell with a comma or a quote (RFC 4180)', () => {
    const text = spoolsCsv([spool], ['number', 'colour_name', 'note'])
    expect(text).toBe('number,colour_name,note\r\n12,"Red, Matte","Bought at ""Micro Center"", aisle 4"\r\n')
  })

  it('writes the effective values, the names, the type code and the derived columns', () => {
    const text = spoolsCsv([spool], EXPORT_COLUMNS, new Map([['t1', 'BL-PLA-RED']]))
    const [header, row] = parseCsv(text)
    const cells = Object.fromEntries(header!.map((name, at) => [name, row![at]]))
    expect(cells).toMatchObject({
      number: '12',
      type: 'BL-PLA-RED',
      material: 'PLA',
      brand: 'Bambu Lab',
      rgba: '#FF000080',
      extra_colours: '#00FF00FF #0000FFFF',
      location: 'Shelf A',
      weight_locked: 'false',
      grams_left: '640',
      low_stock: 'false',
    })
    expect(header).toEqual([...SPOOL_CSV_COLUMNS, ...DERIVED_CSV_COLUMNS])
  })

  it('reads an export back as rows, ignoring the derived columns and naming unknown ones', () => {
    const text = '﻿number,note,grams_left,colour\r\n12,"a, b",640,red\n,"line\nbreak",,\n'
    const read = readSpoolsCsv(text)
    expect(read.rows).toEqual([{ number: '12', note: 'a, b' }, { note: 'line\nbreak' }])
    expect(read.ignored).toEqual(['grams_left'])
    expect(read.unknown).toEqual(['colour'])
  })

  it('reads an empty file as no rows', () => {
    expect(readSpoolsCsv('').rows).toEqual([])
  })

  it('reads back a note that already starts with a quote and a formula start unchanged', () => {
    for (const note of ["'=x", "''=x", "'+1"]) {
      const text = spoolsCsv([{ ...spool, note } as Spool], ['number', 'note'])
      expect(readSpoolsCsv(text).rows).toEqual([{ number: '12', note }])
    }
  })

  it('guards a formula-like text cell with a quote and reads it back unchanged', () => {
    const formula = { ...spool, note: '=HYPERLINK("http://x","y")' } as Spool
    const text = spoolsCsv([formula], ['number', 'note'])
    expect(text).toBe('number,note\r\n12,"\'=HYPERLINK(""http://x"",""y"")"\r\n')
    expect(readSpoolsCsv(text).rows).toEqual([{ number: '12', note: '=HYPERLINK("http://x","y")' }])
    for (const lead of ['+1', '-1', '@sum', '\tx']) {
      const [, row] = parseCsv(spoolsCsv([{ ...spool, note: lead } as Spool], ['note']))
      expect(row![0]).toBe(`'${lead}`)
    }
  })

  it('leaves a number, even a negative one, and an ordinary quote alone', () => {
    const negative = { ...spool, effective: { ...spool.effective, cost_per_kg: '-5' }, note: "'Tis fine" } as unknown as Spool
    const text = spoolsCsv([negative], ['cost_per_kg', 'note'])
    expect(text).toBe("cost_per_kg,note\r\n-5,'Tis fine\r\n")
    expect(readSpoolsCsv(text).rows).toEqual([{ cost_per_kg: '-5', note: "'Tis fine" }])
  })
})
