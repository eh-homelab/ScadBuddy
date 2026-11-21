/**
 * #6262 — the spools CSV (plan F24, F25): what the Spools tab exports, and how it reads a
 * file back into the rows `POST /filament/spools/import` takes. The browser parses the
 * file and sends its cells as JSON; no file bytes go to the server. RFC 4180 throughout
 * (https://www.rfc-editor.org/rfc/rfc4180): CRLF line ends, a cell with a comma, a quote
 * or a line break in double quotes, a quote doubled.
 */
import type { CsvRow, Spool } from '../../api/filament'

/**
 * The CSV's own columns, the `CsvRow` schema's keys in `filament/csv_import.py` `COLUMNS`
 * order. An import reads these; the check below fails the typecheck when the schema gains
 * one this list does not name (F25).
 */
export const SPOOL_CSV_COLUMNS = [
  'number',
  'type',
  'material',
  'subtype',
  'brand',
  'category',
  'effect',
  'colour_name',
  'rgba',
  'extra_colours',
  'diameter_mm',
  'density_g_cm3',
  'nozzle_temp_min',
  'nozzle_temp_max',
  'bed_temp_min',
  'bed_temp_max',
  'label_weight_g',
  'insert_type',
  'low_stock_pct',
  'cost_per_kg',
  'weight_locked',
  'location',
  'note',
] as const satisfies readonly (keyof CsvRow)[]
const EVERY_COLUMN: Exclude<keyof CsvRow, (typeof SPOOL_CSV_COLUMNS)[number]> extends never ? true : never = true
void EVERY_COLUMN

/** What an export adds that an import ignores (`csv_import.py` `DERIVED_COLUMNS`, F24c). */
export const DERIVED_CSV_COLUMNS = [
  'grams_left',
  'last_weighed_g',
  'last_weighed_at',
  'last_dried_at',
  'last_used_at',
  'low_stock',
  'archived_at',
] as const

export type CsvColumn = (typeof SPOOL_CSV_COLUMNS)[number] | (typeof DERIVED_CSV_COLUMNS)[number]
export const EXPORT_COLUMNS: readonly CsvColumn[] = [...SPOOL_CSV_COLUMNS, ...DERIVED_CSV_COLUMNS]

type Cell = string | number | boolean | null | undefined

/** A spool's cell: its effective value (what it shows), the names behind its ids, the type's code. */
function cell(spool: Spool, column: CsvColumn, typeCodes: ReadonlyMap<string, string>): Cell {
  const effective = (spool.effective ?? {}) as Record<string, Cell | string[]>
  const names = spool.names
  switch (column) {
    case 'type':
      return spool.type_id ? (typeCodes.get(spool.type_id) ?? null) : null
    case 'material':
    case 'subtype':
    case 'brand':
    case 'category':
    case 'effect':
    case 'insert_type':
    case 'location':
      return names?.[column] ?? null
    case 'extra_colours': {
      const colours = effective.extra_colours
      return Array.isArray(colours) ? colours.join(' ') : null
    }
    case 'number':
    case 'weight_locked':
    case 'note':
    case 'grams_left':
    case 'last_weighed_g':
    case 'last_weighed_at':
    case 'last_dried_at':
    case 'last_used_at':
    case 'low_stock':
    case 'archived_at':
      return spool[column]
    default: {
      const value = effective[column]
      return Array.isArray(value) ? value.join(' ') : value
    }
  }
}

/** Columns that hold a number: a leading minus there is a sign, not a formula. */
const NUMERIC_COLUMNS = new Set<CsvColumn>([
  'number',
  'diameter_mm',
  'density_g_cm3',
  'nozzle_temp_min',
  'nozzle_temp_max',
  'bed_temp_min',
  'bed_temp_max',
  'label_weight_g',
  'low_stock_pct',
  'cost_per_kg',
  'grams_left',
  'last_weighed_g',
])
/** What makes a spreadsheet read a cell as a formula (OWASP, "CSV Injection"). */
const FORMULA_START = /^[=+\-@\t\r]/
/** A formula start behind any number of `'`: what the import's one-`'` strip would damage. */
const QUOTED_FORMULA = /^'+[=+\-@\t\r]/

/**
 * A cell as CSV text. A text cell that starts like a formula (or like `'` and a formula) gets a leading `'`, so a
 * spreadsheet shows it as text (CSV injection,
 * https://owasp.org/www-community/attacks/CSV_Injection); `readSpoolsCsv` removes it again.
 */
function quote(value: Cell, column: CsvColumn): string {
  if (value === null || value === undefined) return ''
  let text = String(value)
  if (!NUMERIC_COLUMNS.has(column) && (FORMULA_START.test(text) || QUOTED_FORMULA.test(text))) text = `'${text}`
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
}

/** The spools as CSV text under `columns`, one header line then a line per spool. */
export function spoolsCsv(spools: readonly Spool[], columns: readonly CsvColumn[], typeCodes: ReadonlyMap<string, string> = new Map()): string {
  const lines = [columns.join(','), ...spools.map((spool) => columns.map((column) => quote(cell(spool, column, typeCodes), column)).join(','))]
  return lines.map((line) => `${line}\r\n`).join('')
}

/** RFC 4180 records; CRLF or LF line ends, a leading byte-order mark dropped. */
export function parseCsv(text: string): string[][] {
  const records: string[][] = []
  let record: string[] = []
  let field = ''
  let quoted = false
  let at = text.startsWith('﻿') ? 1 : 0
  const endRecord = () => {
    record.push(field)
    field = ''
    if (record.length > 1 || record[0] !== '') records.push(record)
    record = []
  }
  for (; at < text.length; at += 1) {
    const char = text[at]!
    if (quoted) {
      if (char === '"' && text[at + 1] === '"') {
        field += '"'
        at += 1
      } else if (char === '"') quoted = false
      else field += char
    } else if (char === '"') quoted = true
    else if (char === ',') {
      record.push(field)
      field = ''
    } else if (char === '\n') endRecord()
    else if (char === '\r') {
      if (text[at + 1] === '\n') at += 1
      endRecord()
    } else field += char
  }
  if (field !== '' || record.length) endRecord()
  return records
}

export interface ReadCsv {
  /** One object per line after the header, its non-empty cells by column. */
  rows: CsvRow[]
  /** Derived columns the file has, which the import ignores. */
  ignored: string[]
  /** Columns that are neither the CSV's own nor derived: the import would refuse them. */
  unknown: string[]
}

const OWN = new Set<string>(SPOOL_CSV_COLUMNS)
const DERIVED = new Set<string>(DERIVED_CSV_COLUMNS)

/** Undoes the export's `'` guard: one leading `'` before a formula start, in a text column. */
function unprefix(column: string, value: string): string {
  return !NUMERIC_COLUMNS.has(column as CsvColumn) && value.startsWith("'") && (FORMULA_START.test(value.slice(1)) || QUOTED_FORMULA.test(value.slice(1))) ? value.slice(1) : value
}

/** A spools CSV's rows, as the import route takes them. */
export function readSpoolsCsv(text: string): ReadCsv {
  const [header = [], ...records] = parseCsv(text)
  const names = header.map((name) => name.trim())
  const rows = records.map((record) => {
    const row: Record<string, string> = {}
    names.forEach((name, at) => {
      const value = record[at] ?? ''
      if (OWN.has(name) && value.trim() !== '') row[name] = unprefix(name, value)
    })
    return row as CsvRow
  })
  return {
    rows,
    ignored: names.filter((name) => DERIVED.has(name)),
    unknown: names.filter((name) => name !== '' && !OWN.has(name) && !DERIVED.has(name)),
  }
}
