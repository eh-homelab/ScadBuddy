import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { getSpool } from '../../api/filament'
import {
  addPrice,
  clearShopping,
  confirmInvoice,
  createInvoice,
  createSupplier,
  deleteInvoice,
  deletePrice,
  getInvoice,
  getPriceSheet,
  getSupplierStats,
  invoiceConflictId,
  listInvoices,
  listPrices,
  listShopping,
  listSuppliers,
  listTypePrices,
  patchInvoice,
  patchShopping,
  putInvoiceLines,
  invoiceFileUrl,
} from '../../api/filamentPurchasing'
import { resetMockState } from '../handlers'
import { T } from './filament/seed'
import { INVOICE, ITEM, LINE, PRODUCT, SUPPLIER } from './filamentPurchasingSeed'

const refusal = async (promise: Promise<unknown>) => {
  const error = await promise.then(
    () => null,
    (caught: unknown) => caught,
  )
  expect(error).toBeInstanceOf(ApiError)
  return error as ApiError
}

describe('the purchasing mocks', () => {
  it('seeds 3 suppliers, 2 drafts, 2 confirmed invoices and a shopping list of 4 with one purchased', async () => {
    resetMockState()
    expect((await listSuppliers()).map((row) => [row.name, row.product_count])).toEqual([
      ['Amazon', 1],
      ['Bambu Lab', 3],
      ['Micro Center', 1],
    ])
    expect((await listInvoices({ status: 'draft' })).length).toBe(2)
    expect((await listInvoices({ status: 'confirmed' })).length).toBe(2)
    const list = await listShopping()
    expect(list.length).toBe(4)
    expect(list.filter((item) => item.status === 'purchased').map((item) => item.id)).toEqual([ITEM.CLEAR])
    expect(list.at(-1)?.status).toBe('purchased')
  })

  it('works out tax and cost exactly, as the backend does', async () => {
    resetMockState()
    const draft = await getInvoice(INVOICE.DRAFT_A)
    expect(draft.difference).toBe('0.0000')
    expect(draft.reconciles).toBe(true)
    expect(draft.lines.map((line) => [line.line_tax, line.allocated_cost, line.tax_basis])).toEqual([
      ['3.2957', '43.2757', 'receipt-wide 8.2%'],
      ['3.9543', '51.9243', 'receipt-wide 8.2%'],
    ])
    expect(draft.lines[0]!.unit_cost).toBe('21.6379')
  })

  it('refuses a mixed line tax and a fifth decimal with 422', async () => {
    resetMockState()
    const line = { description: 'x', quantity: 1, unit_price: '10' }
    const base = { supplier_id: SUPPLIER.BAMBU, invoice_date: '2026-11-16', currency: 'USD', subtotal: '20', total: '20' }
    expect((await refusal(createInvoice({ ...base, lines: [{ ...line, line_tax: '1' }, line] }))).status).toBe(422)
    expect((await refusal(createInvoice({ ...base, subtotal: '20.00001', lines: [line] }))).status).toBe(422)
  })

  it('refuses a duplicate number with 409 and the first invoice', async () => {
    resetMockState()
    const body = { supplier_id: SUPPLIER.BAMBU, invoice_date: '2026-11-16', currency: 'USD', subtotal: '0', total: '0', number: 'US-441907' }
    const error = await refusal(createInvoice(body))
    expect(error.status).toBe(409)
    expect(invoiceConflictId(error)).toBe(INVOICE.CONFIRMED_A)
    const made = await createInvoice({ ...body, number: 'NEW-1' })
    expect(invoiceConflictId(await refusal(patchInvoice(made.id, { number: 'US-441907' })))).toBe(INVOICE.CONFIRMED_A)
  })

  it('freezes a confirmed invoice but for its note', async () => {
    resetMockState()
    expect((await patchInvoice(INVOICE.CONFIRMED_A, { note: 'filed' })).note).toBe('filed')
    const frozen = await refusal(patchInvoice(INVOICE.CONFIRMED_A, { total: '1' }))
    expect([frozen.status, invoiceConflictId(frozen)]).toEqual([409, null])
    expect((await refusal(putInvoiceLines(INVOICE.CONFIRMED_A, []))).status).toBe(409)
    expect((await refusal(deleteInvoice(INVOICE.CONFIRMED_A))).status).toBe(409)
  })

  it('refuses a wrong currency, an empty draft and an unreconciled total on confirm, writing nothing', async () => {
    resetMockState()
    const base = { supplier_id: SUPPLIER.BAMBU, invoice_date: '2026-11-16', subtotal: '10', total: '10' }
    const eur = await createInvoice({ ...base, currency: 'EUR', lines: [{ description: 'x', quantity: 1, unit_price: '10', type_id: T.JADE }] })
    expect((await refusal(confirmInvoice(eur.id))).detail).toContain("shop's currency is USD")
    const empty = await createInvoice({ ...base, currency: 'USD' })
    expect((await refusal(confirmInvoice(empty.id))).detail).toBe('an invoice with no lines cannot be confirmed')
    const off = await createInvoice({ ...base, currency: 'USD', total: '12', lines: [{ description: 'x', quantity: 1, unit_price: '10', type_id: T.JADE }] })
    expect((await refusal(confirmInvoice(off.id))).detail).toContain('away from the lines plus shipping')
    expect((await getInvoice(off.id)).status).toBe('draft')
  })

  it('confirms: spools with the cost per kg, a price point per line, and the spool page shows both', async () => {
    resetMockState()
    const done = await confirmInvoice(INVOICE.DRAFT_A)
    expect(done.status).toBe('confirmed')
    const [orange, jade] = done.lines
    expect([orange!.spool_ids.length, jade!.spool_ids.length]).toEqual([2, 3])
    const spool = await getSpool(jade!.spool_ids[0]!)
    expect(spool.cost_per_kg).toBe('17.3081')
    expect(spool.invoice).toMatchObject({ invoice_id: INVOICE.DRAFT_A, number: 'US-452318', supplier_name: 'Bambu Lab', line_id: LINE.DRAFT_A2, line_position: 2, unit_cost: '17.3081' })
    expect(spool.type_suppliers.map((row) => [row.supplier_name, row.sku])).toEqual([['Bambu Lab', 'A00-W1']])
    expect((await listPrices(PRODUCT.JADE))[0]).toMatchObject({ source: 'invoice', on_date: '2026-11-12', price: '17.3081' })
    expect((await confirmInvoice(INVOICE.DRAFT_A)).lines[0]!.spool_ids).toEqual(orange!.spool_ids)
  })

  it('makes the type a line proposes when it confirms', async () => {
    resetMockState()
    const done = await confirmInvoice(INVOICE.DRAFT_B)
    const proposed = done.lines[1]!
    expect(proposed.type_id).not.toBeNull()
    expect((await getSpool(proposed.spool_ids[0]!)).type_id).toBe(proposed.type_id)
  })

  it('keeps a manual price point deletable and an invoice one not', async () => {
    resetMockState()
    const made = await addPrice(PRODUCT.JADE, { price: '16.49' })
    expect(made.source).toBe('manual')
    expect(made.price_per_kg).toBe('16.4900')
    await deletePrice(made.id)
    const fromInvoice = (await listPrices(PRODUCT.JADE)).find((price) => price.source === 'invoice')
    expect(fromInvoice).toBeDefined()
    expect((await refusal(deletePrice(fromInvoice!.id))).status).toBe(409)
  })

  it('reads the price sheet, a type\'s price history and the stats', async () => {
    resetMockState()
    const sheet = await getPriceSheet(SUPPLIER.BAMBU)
    const jade = sheet.find((row) => row.product.id === PRODUCT.JADE)!
    expect(jade.change_pct).toBe('8.2')
    expect((await listTypePrices(T.TPU_RED)).map((price) => [price.supplier_name, price.price_per_kg])).toEqual([
      ['Amazon', '32.4600'],
      ['Amazon', '28.9900'],
    ])
    expect((await getSupplierStats()).map((row) => [row.supplier_name, row.spool_count, row.spend])).toEqual([
      ['Amazon', 1, '21.2200'],
      ['Bambu Lab', 2, '34.6100'],
    ])
  })

  it('refuses a duplicate supplier name and clears the purchased items', async () => {
    resetMockState()
    expect((await refusal(createSupplier({ name: 'amazon' }))).status).toBe(409)
    expect((await refusal(createSupplier({ name: 'Shop', website: 'javascript:alert(1)' }))).status).toBe(422)
    await patchShopping(ITEM.PINK, { status: 'purchased' })
    expect(await clearShopping({ purchasedOnly: true })).toEqual({ removed: 2 })
    expect((await listShopping()).length).toBe(2)
  })

  it('takes a PDF file and refuses a text one', async () => {
    resetMockState()
    // A text body, as `printAttachments.ts`: jsdom's `FormData` does not go through Node's fetch.
    const send = (name: string, type: string) =>
      fetch(`/api/v1/filament/invoices/${INVOICE.DRAFT_B}/file`, {
        method: 'POST',
        headers: { 'Content-Type': 'multipart/form-data; boundary=x' },
        body: `--x\r\nContent-Disposition: form-data; name="file"; filename="${name}"\r\nContent-Type: ${type}\r\n\r\n%PDF-1.4\r\n--x--\r\n`,
      })
    expect((await send('a.txt', 'text/plain')).status).toBe(422)
    const done = await send('receipt.pdf', 'application/pdf')
    expect(await done.json()).toMatchObject({ name: 'receipt.pdf', media_type: 'application/pdf', size: 8 })
    expect((await getInvoice(INVOICE.DRAFT_B)).file?.name).toBe('receipt.pdf')
    expect(invoiceFileUrl(INVOICE.DRAFT_B)).toBe(`/api/v1/filament/invoices/${INVOICE.DRAFT_B}/file`)
  })
})
