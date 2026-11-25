import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import type { SpoolDetail } from '../../api/filament'
import { BIG_LEADING, BOLD_FIELDS, CODE_MM, FIELD_MM, fitLines, GAP_MM, type LabelField, LEADING, NUMBER_MM, PAD_MM, PLACE_NAME_MM, QR_MM, QUIET_MM, spoolLines } from './labelSheet'

/**
 * #7216 R-7216-19: `labelSheet.ts`'s fit constants and `fitLines` against the shared vectors, which
 * `backend/tests/filament/test_label_fit_vectors.py` runs against `label_pdf.py` too, so the browser label
 * keeps the PDF's lines (R-7216-10).
 */
type Case = { name: string; height_mm: number; fields: LabelField[]; values: LabelField[]; kept: LabelField[] }
const { constants, cases } = JSON.parse(readFileSync(`${import.meta.dirname}/../../../../test-fixtures/filament/label-fit-vectors.json`, 'utf8')) as {
  constants: Record<string, unknown>
  cases: Case[]
}

/** A spool with a value for each field in `values` only (the number always has one). */
function spoolWith(values: readonly LabelField[]): SpoolDetail {
  const has = (field: LabelField) => values.includes(field)
  return {
    id: 's1',
    number: 12,
    colour_name: null,
    note: has('note') ? 'Dry first' : null,
    created_at: has('added') ? '2026-11-20T12:00:00Z' : null,
    effective: {
      colour_name: has('colour') ? 'Jade White' : null,
      rgba: has('hex') ? '#00ae42ff' : null,
      nozzle_temp_min: has('temps') ? 190 : null,
      nozzle_temp_max: has('temps') ? 230 : null,
      label_weight_g: has('weight') ? 1000 : null,
    },
    names: { material: has('material') ? 'PLA' : null, subtype: has('material') ? 'Basic' : null, brand: has('brand') ? 'Bambu Lab' : null, location: has('location') ? 'Shelf A' : null },
  } as unknown as SpoolDetail
}

describe('the label fit vectors (#7216, R-7216-19)', () => {
  it('has the shared constants', () => {
    expect({ PAD_MM, QR_MM, QUIET_MM, NUMBER_MM, CODE_MM, PLACE_NAME_MM, LEADING, BIG_LEADING, GAP_MM, FIELD_MM, BOLD_FIELDS: [...BOLD_FIELDS].sort() }).toEqual({
      ...constants,
      BOLD_FIELDS: [...(constants.BOLD_FIELDS as string[])].sort(),
    })
  })

  it.each(cases)('$name', ({ height_mm, fields, values, kept }) => {
    expect(fitLines(spoolLines(spoolWith(values), fields), height_mm).map((line) => line.field)).toEqual(kept)
  })
})
