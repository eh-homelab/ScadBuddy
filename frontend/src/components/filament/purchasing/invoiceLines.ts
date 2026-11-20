import type { Spool } from '../../../api/filament'
import type { InvoiceLine, InvoiceLineIn } from '../../../api/filamentPurchasing'
import { parseDecimal } from '../../../lib/decimal'

/** A PDF or a photo, by the type the browser reports; the server's sniff is the truth. */
export const acceptableInvoiceFile = (file: File) => file.type === 'application/pdf' || file.type.startsWith('image/')

/** One line of a draft as the person is typing it: every field is the text of its input. */
export interface LineDraft {
  /** Stable while the line is on screen; the server makes new line ids at every save. */
  key: string
  description: string
  sku: string
  colourCode: string
  quantity: string
  unitPrice: string
  /** Empty on every line means the receipt's tax is spread over them. */
  lineTax: string
  /** `''` for none. */
  typeId: string
  /** `''` for none: the server matches by SKU at confirm. */
  productId: string
  /** A type the line proposes (an import), made at confirm; kept as it came. */
  proposedType: InvoiceLine['proposed_type']
  updateSpoolIds: string[]
}

export type LineField = 'description' | 'quantity' | 'unitPrice' | 'lineTax'
export type LineErrors = Record<string, Partial<Record<LineField, string>>>

let counter = 0
const nextKey = () => `line-${++counter}`

export function draftOfLine(line: InvoiceLine): LineDraft {
  return {
    key: nextKey(),
    description: line.description,
    sku: line.sku ?? '',
    colourCode: line.colour_code ?? '',
    quantity: String(line.quantity),
    unitPrice: line.unit_price,
    lineTax: line.tax_basis === 'line' ? line.line_tax : '',
    typeId: line.type_id ?? '',
    productId: line.supplier_product_id ?? '',
    proposedType: line.proposed_type,
    updateSpoolIds: line.update_spool_ids,
  }
}

export const blankLine = (): LineDraft => ({
  key: nextKey(),
  description: '',
  sku: '',
  colourCode: '',
  quantity: '1',
  unitPrice: '',
  lineTax: '',
  typeId: '',
  productId: '',
  proposedType: null,
  updateSpoolIds: [],
})

/** A line has a type when it names one or proposes one (made at confirm). */
export const hasType = (row: LineDraft) => row.typeId !== '' || row.proposedType != null

const quantityOf = (row: LineDraft): number | null => (/^\d+$/.test(row.quantity.trim()) && Number(row.quantity) >= 1 && Number(row.quantity) <= 1_000 ? Number(row.quantity) : null)

/** What is wrong with each typed line, by line key; a line with nothing wrong has no entry. */
export function lineErrors(rows: LineDraft[]): LineErrors {
  const errors: LineErrors = {}
  const some = rows.some((row) => row.lineTax.trim() !== '')
  for (const row of rows) {
    const found: LineErrors[string] = {}
    if (!row.description.trim()) found.description = 'Describe the line.'
    if (quantityOf(row) === null) found.quantity = 'Enter a whole number from 1 to 1000.'
    if (parseDecimal(row.unitPrice) === null) found.unitPrice = 'Enter a price such as 12.50.'
    if (row.lineTax.trim() !== '' && parseDecimal(row.lineTax) === null) found.lineTax = 'Enter a tax such as 1.40, or leave it empty.'
    else if (some && row.lineTax.trim() === '') found.lineTax = 'Give every line a tax, or none.'
    if (Object.keys(found).length) errors[row.key] = found
  }
  return errors
}

/** The body of `putInvoiceLines`; call it only for lines `lineErrors` passes. */
export function bodyOfLines(rows: LineDraft[]): InvoiceLineIn[] {
  return rows.map((row) => ({
    description: row.description.trim(),
    sku: row.sku.trim() || null,
    colour_code: row.colourCode.trim() || null,
    quantity: quantityOf(row)!,
    unit_price: parseDecimal(row.unitPrice)!,
    line_tax: row.lineTax.trim() === '' ? null : parseDecimal(row.lineTax),
    type_id: row.typeId || null,
    supplier_product_id: row.productId || null,
    proposed_type: row.typeId ? null : (row.proposedType as InvoiceLineIn['proposed_type']),
    update_spool_ids: row.updateSpoolIds,
  }))
}

/** What a confirm will do, for its dialog: new spools, spools filled in, one price point per line. */
export function confirmCounts(rows: LineDraft[]): { made: number; filled: number; prices: number } {
  let made = 0
  let filled = 0
  for (const row of rows) {
    const quantity = quantityOf(row) ?? 0
    const fills = Math.min(row.updateSpoolIds.length, quantity)
    filled += fills
    made += quantity - fills
  }
  return { made, filled, prices: rows.length }
}

/**
 * The spools on no invoice line that a line of `typeId` may fill: of that type, or with none.
 * Those with no cost come first; a costed one's cost is replaced at confirm (#6700). An archived
 * (used-up) spool is one too: it was still bought, and the confirm leaves it archived (#6707).
 */
export function fillable(spools: Spool[], typeId: string, taken: ReadonlySet<string>): Spool[] {
  const open = spools.filter((spool) => !spool.invoice_line_id && !taken.has(spool.id) && (spool.type_id === null || (typeId !== '' && spool.type_id === typeId)))
  return [...open.filter((spool) => spool.cost_per_kg === null), ...open.filter((spool) => spool.cost_per_kg !== null)]
}

/** The invoice's tax basis from its lines': one shared basis, or "per line"; `null` when no tax. */
export function taxBasisOf(lines: Pick<InvoiceLine, 'tax_basis'>[]): string | null {
  const bases = [...new Set(lines.map((line) => line.tax_basis))]
  if (bases.length !== 1 || bases[0] === 'none') return bases.length > 1 ? 'mixed' : null
  return bases[0] === 'line' ? 'per line' : bases[0]!
}
