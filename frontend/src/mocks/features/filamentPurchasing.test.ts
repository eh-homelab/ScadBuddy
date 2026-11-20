import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { createSpool, getSpool, listSpoolEvents, listSpools } from '../../api/filament'
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
  patchProduct,
  patchSupplier,
  patchShopping,
  putInvoiceLines,
  runningImportId,
  startInvoiceImport,
  invoiceFileUrl,
  uploadInvoiceFile,
} from '../../api/filamentPurchasing'
import { resetMockState } from '../handlers'
import { M, spoolId, T } from './filament/seed'
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
  it('starts an invoice import as the agent route does: a session, then 409 import_running, 409 confirmed, 422', async () => {
    resetMockState()
    const first = await startInvoiceImport(INVOICE.DRAFT_A)
    expect((await getInvoice(INVOICE.DRAFT_A)).import_run_id).toBe(first.session_id)
    const again = await refusal(startInvoiceImport(INVOICE.DRAFT_A))
    expect(again.status).toBe(409)
    expect(runningImportId(again)).toBe(first.session_id)
    expect((await refusal(startInvoiceImport(INVOICE.CONFIRMED_A))).problem).toMatchObject({ type: 'confirmed' })
    expect((await refusal(startInvoiceImport(INVOICE.DRAFT_B))).status).toBe(422)
  })

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

  describe('a line that fills existing spools (Decision 11)', () => {
    const header = { supplier_id: SUPPLIER.BAMBU, invoice_date: '2026-11-16', currency: 'USD', subtotal: '30', total: '30' }
    const draft = (fill: string[], quantity = 2, type_id: string | null = T.JADE) =>
      createInvoice({ ...header, lines: [{ description: 'Jade', quantity, unit_price: String(30 / quantity), type_id, update_spool_ids: fill }] })

    it('fills a needs-type spool and makes only the rest as new spools', async () => {
      resetMockState()
      const typeless = spoolId(6)
      const done = await confirmInvoice((await draft([typeless])).id)
      const line = done.lines[0]!
      expect(line.spool_ids.length).toBe(2)
      expect(line.spool_ids[0]).toBe(typeless)
      const filled = await getSpool(typeless)
      expect(filled).toMatchObject({ type_id: T.JADE, cost_per_kg: '15.0000' })
      expect(filled.invoice).toMatchObject({ line_id: line.id, unit_cost: '15.0000' })
      const all = await listSpools({ includeArchived: true })
      expect(all.length).toBe(13)
    })

    it('fills a costed spool with no invoice line and replaces its cost, with one edited event (#6700)', async () => {
      resetMockState()
      const costed = await createSpool({ type_id: T.JADE, cost_per_kg: '10', weight_locked: false })
      expect(costed.invoice_line_id).toBeNull()
      const done = await confirmInvoice((await draft([costed.id])).id)
      expect(done.lines[0]!.spool_ids[0]).toBe(costed.id)
      const listed = (await listSpools({ includeArchived: true })).find((one) => one.id === costed.id)
      expect(listed?.invoice_line_id).toBe(done.lines[0]!.id)
      expect(await getSpool(costed.id)).toMatchObject({ cost_per_kg: '15.0000' })
      const edited = (await listSpoolEvents(costed.id)).filter((event) => event.kind === 'edited')
      expect(edited.length).toBe(1)
      expect(edited[0]!.data).toEqual({ changes: { cost_per_kg: ['10', '15.0000'] } })
    })

    it('fills an archived spool with the line cost only: it stays archived and keeps its type (#6707)', async () => {
      resetMockState()
      const spent = spoolId(19)
      const before = await getSpool(spent)
      expect(before.archived_at).not.toBeNull()
      const done = await confirmInvoice((await draft([spent], 2, T.INLAND_BLACK)).id)
      const [filled, made] = done.lines[0]!.spool_ids
      expect(filled).toBe(spent)
      const after = await getSpool(spent)
      expect(after.archived_at).toBe(before.archived_at)
      expect(after.type_id).toBe(before.type_id)
      expect(after.cost_per_kg).toBe((await getSpool(made!)).cost_per_kg)
      expect(after.cost_per_kg).not.toBe(before.cost_per_kg)
    })

    it('refuses an invoiced or other-type spool, too many ids and a duplicate, naming the line', async () => {
      resetMockState()
      const cases: [string[], string, number?][] = [
        [[spoolId(1)], 'already belongs to an invoice line'],
        [[spoolId(9)], 'has another spool type'],
        [[spoolId(6), spoolId(6)], 'it names a spool twice'],
      ]
      for (const [ids, text] of cases) {
        const made = await draft(ids)
        const error = await refusal(confirmInvoice(made.id))
        expect(error.status).toBe(409)
        expect(error.detail).toContain('line 1: ')
        expect(error.detail).toContain(text)
        expect((await getInvoice(made.id)).status).toBe('draft')
      }
      expect((await getSpool(spoolId(6))).type_id).toBeNull()
    })

    it('refuses more spools than the quantity when the lines are written, as the backend does', async () => {
      resetMockState()
      const error = await refusal(draft([spoolId(6), spoolId(11), spoolId(7)]))
      expect(error.status).toBe(422)
      expect(error.detail).toContain('more spools than')
    })

    it("prices a filled spool from the type's label weight when its own is 0", async () => {
      resetMockState()
      const spool = await createSpool({ material_id: M.PLA, label_weight_g: 0, weight_locked: false })
      const done = await confirmInvoice((await draft([spool.id], 1, T.JADE)).id)
      expect(done.lines[0]!.spool_ids[0]).toBe(spool.id)
      expect((await getSpool(spool.id)).cost_per_kg).toBe('30.0000')
    })

    it('refuses one spool named by two lines of the same invoice', async () => {
      resetMockState()
      const line = { description: 'Jade', quantity: 1, unit_price: '15', type_id: T.JADE, update_spool_ids: [spoolId(6)] }
      const made = await createInvoice({ ...header, lines: [line, line] })
      expect((await refusal(confirmInvoice(made.id))).detail).toBe(`line 2: spool ${spoolId(6)} already belongs to an invoice line`)
    })
  })

  it('refuses a line whose supplier product cannot be used, writing nothing', async () => {
    resetMockState()
    const before = (await listSpools({ includeArchived: true })).length
    const base = { supplier_id: SUPPLIER.BAMBU, invoice_date: '2026-11-16', currency: 'USD', subtotal: '10', total: '10' }
    const named = await createInvoice({ ...base, lines: [{ description: 'x', quantity: 1, unit_price: '10', type_id: T.PINK, supplier_product_id: PRODUCT.JADE }] })
    expect((await refusal(confirmInvoice(named.id))).detail).toBe('line 1: its supplier product is for another spool type')
    const bySku = await createInvoice({ ...base, lines: [{ description: 'x', sku: 'a00-w1', quantity: 1, unit_price: '10', type_id: T.PINK }] })
    expect((await refusal(confirmInvoice(bySku.id))).detail).toBe('line 1: its supplier product is for another spool type')
    const other = await createInvoice({ ...base, lines: [{ description: 'x', quantity: 1, unit_price: '10', type_id: T.INLAND_BLACK, supplier_product_id: PRODUCT.INLAND }] })
    expect((await refusal(confirmInvoice(other.id))).detail).toBe("line 1: its supplier product is archived or not this supplier's")
    await patchProduct(PRODUCT.ORANGE, { archived: true })
    const archived = await createInvoice({ ...base, lines: [{ description: 'x', quantity: 1, unit_price: '10', type_id: T.ORANGE, supplier_product_id: PRODUCT.ORANGE }] })
    expect((await refusal(confirmInvoice(archived.id))).detail).toBe("line 1: its supplier product is archived or not this supplier's")
    expect((await listSpools({ includeArchived: true })).length).toBe(before)
  })

  it('refuses an invoice for an archived supplier, and confirming one that became archived', async () => {
    resetMockState()
    const base = { invoice_date: '2026-11-16', currency: 'USD', subtotal: '10', total: '10', lines: [{ description: 'x', quantity: 1, unit_price: '10', type_id: T.JADE }] }
    const open = await createInvoice({ ...base, supplier_id: SUPPLIER.AMAZON })
    await patchSupplier(SUPPLIER.AMAZON, { archived: true })
    const made = await refusal(createInvoice({ ...base, supplier_id: SUPPLIER.AMAZON }))
    expect(made.status).toBe(422)
    expect(made.detail).toContain('archived')
    const confirmed = await refusal(confirmInvoice(open.id))
    expect(confirmed.status).toBe(409)
    expect(confirmed.detail).toContain('archived')
    expect((await getInvoice(open.id)).status).toBe('draft')
  })

  it('refuses a quantity over 1,000 with 422', async () => {
    resetMockState()
    const line = { description: 'x', quantity: 1001, unit_price: '1', type_id: T.JADE }
    expect((await refusal(createInvoice({ supplier_id: SUPPLIER.BAMBU, invoice_date: '2026-11-16', currency: 'USD', subtotal: '1', total: '1', lines: [line] }))).status).toBe(422)
  })

  it('writes nothing when a later line is refused', async () => {
    resetMockState()
    const before = (await listSpools({ includeArchived: true })).length
    const made = await createInvoice({
      supplier_id: SUPPLIER.MICRO,
      invoice_date: '2026-11-16',
      currency: 'USD',
      subtotal: '30',
      total: '30',
      lines: [
        { description: 'Good', quantity: 2, unit_price: '10', type_id: T.JADE },
        // No material: the proposed type is invalid.
        { description: 'Bad', quantity: 1, unit_price: '10', proposed_type: { code: 'NEW-1', rgba: '#FFFFFFFF', label_weight_g: 1000 } as never },
      ],
    })
    const error = await refusal(confirmInvoice(made.id))
    expect(error.status).toBe(409)
    expect(error.detail).toContain('line 2: its proposed spool type is invalid')
    expect((await listSpools({ includeArchived: true })).length).toBe(before)
    const after = await getInvoice(made.id)
    expect([after.status, after.lines[0]!.spool_ids, after.lines[0]!.supplier_product_id]).toEqual(['draft', [], null])
    expect((await listPrices(PRODUCT.INLAND)).every((price) => price.source === 'manual')).toBe(true)
  })
})

describe('a draft changing supplier', () => {
  it('moves, drops the products, and is refused as the backend refuses', async () => {
    resetMockState()
    const moved = await patchInvoice(INVOICE.DRAFT_A, { supplier_id: SUPPLIER.AMAZON })
    expect(moved.supplier_id).toBe(SUPPLIER.AMAZON)
    expect(moved.lines.map((line) => line.supplier_product_id)).toEqual([null, null])
    expect((await refusal(patchInvoice(INVOICE.CONFIRMED_A, { supplier_id: SUPPLIER.AMAZON }))).status).toBe(409)
    expect((await refusal(patchInvoice(INVOICE.DRAFT_B, { supplier_id: '00000000-0000-4000-8000-000000000999' }))).status).toBe(422)
    await patchSupplier(SUPPLIER.MICRO, { archived: true })
    expect((await refusal(patchInvoice(INVOICE.DRAFT_A, { supplier_id: SUPPLIER.MICRO }))).status).toBe(422)
    const taken = await createInvoice({ supplier_id: SUPPLIER.BAMBU, number: 'US-452318', invoice_date: '2026-11-01', currency: 'USD', subtotal: '1', total: '1' })
    const clash = await refusal(patchInvoice(INVOICE.DRAFT_A, { supplier_id: SUPPLIER.BAMBU }))
    expect([clash.status, invoiceConflictId(clash)]).toEqual([409, taken.id])
  })

  it('counts the spools each invoice made', async () => {
    resetMockState()
    const counts = new Map((await listInvoices()).map((row) => [row.id, row.spool_count]))
    expect([counts.get(INVOICE.DRAFT_A), counts.get(INVOICE.CONFIRMED_A)]).toEqual([0, 2])
  })
})

describe('uploadInvoiceFile', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('posts the browser file as a FormData `file` part and sets no Content-Type', async () => {
    const fetchStub = vi.fn(() => Promise.resolve(new Response(JSON.stringify({ name: 'a.pdf', media_type: 'application/pdf', size: 3 }), { status: 200 })))
    vi.stubGlobal('fetch', fetchStub)
    const file = new File(['abc'], 'a.pdf', { type: 'application/pdf' })
    await uploadInvoiceFile(INVOICE.DRAFT_B, file)
    const [url, init] = fetchStub.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`/api/v1/filament/invoices/${INVOICE.DRAFT_B}/file`)
    expect(init.method).toBe('POST')
    expect(init.body).toBeInstanceOf(FormData)
    expect((init.body as FormData).get('file')).toBe(file)
    const headers = new Headers(init.headers)
    expect(headers.has('Content-Type')).toBe(false)
  })
})
