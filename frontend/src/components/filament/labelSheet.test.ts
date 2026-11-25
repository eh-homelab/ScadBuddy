import QRCode from 'qrcode'
import { describe, expect, it } from 'vitest'
import type { SpoolDetail } from '../../api/filament'
import { ALL_FIELDS, DEFAULT_FIELDS, fitLines, labelBase, labelPayload, locationPayload, parseLabelPayload, QR_MM, qrSvg, spoolLines } from './labelSheet'

describe('the label payload', () => {
  it('is the spool id behind a fixed prefix, with no host', () => {
    expect(labelPayload('0b9e1c')).toBe('scadbuddy:spool:0b9e1c')
  })

  it('reads the id back, and nothing from other text', () => {
    expect(parseLabelPayload('scadbuddy:spool:abc')).toBe('abc')
    expect(parseLabelPayload('https://x/y')).toBeNull()
    expect(parseLabelPayload('scadbuddy:spool:')).toBeNull()
  })

  it('draws an SVG that differs by payload', async () => {
    const a = await qrSvg(labelPayload('a'))
    const b = await qrSvg(labelPayload('b'))
    expect(a.startsWith('<svg')).toBe(true)
    expect(a).not.toBe(b)
  })
})

describe('the location payload', () => {
  it('builds the address of a code', () => {
    expect(locationPayload('https://sb.example.com/', 'L-12')).toBe('https://sb.example.com/x/L-12')
    expect(locationPayload('https://sb.example.com', 'L-12')).toBe('https://sb.example.com/x/L-12')
  })

  it('keeps the module at least 0.5 mm for the longest base', () => {
    const payload = locationPayload(`https://${'a'.repeat(56)}`, 'L-9999999')
    expect(payload.length - 'L-9999999'.length - 3).toBe(64)
    const { modules } = QRCode.create(payload, { errorCorrectionLevel: 'M' })
    expect(modules.size).toBeLessThanOrEqual(37)
    expect(QR_MM / modules.size).toBeGreaterThanOrEqual(0.5)
  })

  it('takes the base from the setting, else the page origin, with no trailing slash', () => {
    expect(labelBase('https://sb.example.com/')).toBe('https://sb.example.com')
    expect(labelBase(null)).toBe(window.location.origin)
  })
})

/** The spool of the backend's `FULL` label (`backend/tests/filament/test_label_pdf.py`). */
const FULL = {
  id: 's1',
  number: 12,
  colour_name: 'Jade White',
  note: 'Dry first\nthen print',
  created_at: '2026-11-20T12:00:00Z',
  effective: { colour_name: 'Jade White', rgba: '#00ae42ff', nozzle_temp_min: 190, nozzle_temp_max: 230, label_weight_g: 1000 },
  names: { material: 'PLA', subtype: 'Basic', brand: 'Bambu Lab', location: 'Shelf A' },
} as unknown as SpoolDetail

const texts = (lines: { text: string }[]) => lines.map((line) => line.text)

describe('the spool label lines (#7216)', () => {
  it('has the backend fields, with the old set as the default', () => {
    expect(ALL_FIELDS).toEqual(['number', 'material', 'colour', 'hex', 'brand', 'location', 'temps', 'weight', 'note', 'added', 'qr'])
    expect(DEFAULT_FIELDS).toEqual(['number', 'material', 'colour', 'brand', 'qr'])
  })

  it('gives every field the backend text, in its order', () => {
    expect(texts(spoolLines(FULL, ALL_FIELDS))).toEqual(['#12', 'PLA Basic', 'Jade White', '#00AE42', 'Bambu Lab', 'Shelf A', '190–230 °C', '1000 g', 'Dry first', '2026-11-20'])
  })

  it('gives only the chosen fields, in their order, and no number when it is not chosen', () => {
    expect(texts(spoolLines(FULL, ['weight', 'hex']))).toEqual(['#00AE42', '1000 g'])
  })

  it('makes no line for a chosen field with no value', () => {
    const bare = { ...FULL, note: '  ', effective: { colour_name: null }, colour_name: null, names: {} } as unknown as SpoolDetail
    expect(texts(spoolLines(bare, ALL_FIELDS))).toEqual(['#12', '2026-11-20'])
  })

  it('gives one temperature when only one is set or both are equal', () => {
    const one = (min: number | null, max: number | null) => ({ ...FULL, effective: { ...FULL.effective, nozzle_temp_min: min, nozzle_temp_max: max } }) as SpoolDetail
    expect(texts(spoolLines(one(null, 220), ['temps']))).toEqual(['220 °C'])
    expect(texts(spoolLines(one(220, 220), ['temps']))).toEqual(['220 °C'])
  })

  it('dates the added line in UTC', () => {
    const late = { ...FULL, created_at: '2026-11-20T23:30:00-05:00' } as SpoolDetail
    expect(texts(spoolLines(late, ['added']))).toEqual(['2026-11-21'])
  })

  it('uses the backend sizes and weights', () => {
    expect(spoolLines(FULL, ALL_FIELDS).map((line) => [line.field, line.sizeMm, line.bold])).toEqual([
      ['number', 9, true],
      ['material', 3.2, true],
      ['colour', 3, false],
      ['hex', 2.6, false],
      ['brand', 2.6, false],
      ['location', 2.6, false],
      ['temps', 2.6, false],
      ['weight', 2.6, false],
      ['note', 2.6, false],
      ['added', 2.6, false],
    ])
  })
})

describe('the fit of the lines (#7216, R-7216-10)', () => {
  it('keeps the number and leaves off the last lines on a 25 mm label, as the PDF does', () => {
    const kept = fitLines(spoolLines(FULL, ALL_FIELDS), 25)
    // 22 mm of block: the number (9) and its gap (1), the material (4), the colour (3.75) and one
    // small line (3.25) make 21 mm; one more line is 24.25 mm.
    expect(texts(kept)).toEqual(['#12', 'PLA Basic', 'Jade White', '#00AE42'])
    expect(9 + 1 + 3.2 * 1.25 + 3 * 1.25 + 2.6 * 1.25).toBeLessThanOrEqual(25 - 2 * 1.5)
    expect(texts(kept)).not.toContain('Dry first')
    expect(texts(kept)).not.toContain('2026-11-20')
  })

  it('keeps every line on a label tall enough', () => {
    expect(fitLines(spoolLines(FULL, ALL_FIELDS), 55)).toHaveLength(10)
  })

  it('counts the number at its full size, whatever its width', () => {
    const long = { ...FULL, number: 123456 } as SpoolDetail
    expect(fitLines(spoolLines(long, ALL_FIELDS), 25)).toHaveLength(4)
  })

  it('never leaves off a chosen number', () => {
    expect(texts(fitLines(spoolLines(FULL, ['number', 'colour']), 10))).toEqual(['#12'])
  })

  it('counts no gap above the first line when no number is chosen', () => {
    // The material (4), the colour (3.75) and four small lines (13) make 20.75 mm of 22.
    const fields = ALL_FIELDS.filter((field) => field !== 'number')
    expect(texts(fitLines(spoolLines(FULL, fields), 25))).toEqual(['PLA Basic', 'Jade White', '#00AE42', 'Bambu Lab', 'Shelf A', '190–230 °C'])
  })
})
