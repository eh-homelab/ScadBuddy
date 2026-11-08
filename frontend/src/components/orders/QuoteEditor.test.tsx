import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../../api/client'
import type { Quote, SliceQuoteRequest } from '../../api/types'
import { reset, seedUnitCost } from '../../mocks/features/orders/history'
import { printersState } from '../../mocks/features/printers'
import { mockSettings, resetMockState, setMockSettings } from '../../mocks/handlers'
import { FILAMENT_PETG, FILAMENT_PLA, ITEM_DRAFT, ORDER_DRAFT, item } from '../../mocks/features/orders/state'
import { selectOption } from '../../test/select'
import { renderPage } from '../../test/utils'
import { CHECK_DEBOUNCE_MS, QuoteEditor } from './QuoteEditor'
import type { SliceQuoteRun } from './useSliceQuotes'

/** Long enough for a debounced price check to be sent and its answer to land. */
const settled = () => new Promise((resolve) => setTimeout(resolve, 2 * CHECK_DEBOUNCE_MS))

vi.mock('../print/PlateScene', () => ({ PlateScene: () => <div data-testid="plate-scene" /> }))

// #4250 — "Quote from a slice…" opens the print dialog in its quote mode: the spools slice,
// a price-book filament per slot prices, and Quote posts the item's slice quote.
describe('QuoteEditor · a quote from a slice (#4250)', () => {
  beforeEach(() => resetMockState())

  it('hands the dialog’s choices and a filament per slot to the page, and closes (#4693)', async () => {
    const start = vi.fn<(body: SliceQuoteRequest) => Promise<void>>().mockResolvedValue(undefined)
    const draft = item(ITEM_DRAFT, ORDER_DRAFT, 'draft', { quantity: 2 })
    const { user } = renderPage(
      <QuoteEditor orderId={ORDER_DRAFT} itemId={ITEM_DRAFT} currency="USD" exponent={2} quotes={[]} editable onChanged={vi.fn()} item={draft} slice={{ start, dismiss: vi.fn() }} />,
    )
    await user.click(screen.getByTestId('quote-from-slice'))
    const dialog = await screen.findByRole('dialog', { name: 'Quote from a slice' })
    const slots = await within(dialog).findAllByRole('combobox', { name: /^Filament for slot/ })
    expect(slots.length).toBeGreaterThan(0)
    const quote = within(dialog).getByTestId('run-quote')
    for (const [index, slot] of slots.entries()) {
      await selectOption(user, slot, index % 2 === 0 ? /^Bambu PLA/ : /^Bambu PETG/)
    }
    await waitFor(() => expect(quote).toBeEnabled())
    expect(within(dialog).queryByRole('button', { name: 'Print' })).toBeNull()
    await user.click(quote)
    // The dialog does not wait on the operation: its modal would hold the page (#4693).
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Quote from a slice' })).toBeNull())
    expect(start).toHaveBeenCalledTimes(1)
    const body = start.mock.calls[0]?.[0]
    expect(body?.filaments).toEqual(
      Object.fromEntries(slots.map((_, index) => [String(index), index % 2 === 0 ? FILAMENT_PLA : FILAMENT_PETG])),
    )
    expect(body?.filament_plan.slots?.length).toBeGreaterThan(0)
    expect(body).not.toHaveProperty('project_id')
    // #4551: the registry row beside the Bambuddy id, as the run names them.
    const linked = printersState().find((row) => row.bambuddy_printer_id === body?.printer_id)
    expect(linked).toBeDefined()
    expect(body?.printer).toBe(linked?.id)
  })

  // #5544, the PR #5610 review: a library file item before its first quote has no output
  // and no colours, so the dialog opens on the file with an empty filaments map.
  it('slices a library file item with no colours yet, on its file (#5544)', async () => {
    const start = vi.fn<(body: SliceQuoteRequest) => Promise<void>>().mockResolvedValue(undefined)
    const plates = vi.spyOn(api, 'getLibraryPlates')
    const library = {
      ...item(ITEM_DRAFT, ORDER_DRAFT, 'draft'),
      kind: 'library_file' as const,
      reference: null,
      output_id: null,
      colours: [],
      library_file: { id: 120, filename: 'drawer-label.3mf', library: 'bambuddy' as const },
    }
    const { user } = renderPage(
      <QuoteEditor orderId={ORDER_DRAFT} itemId={ITEM_DRAFT} currency="USD" exponent={2} quotes={[]} editable onChanged={vi.fn()} item={library} slice={{ start, dismiss: vi.fn() }} />,
    )
    expect(screen.queryByText(/Render the item first/)).toBeNull()
    await user.click(screen.getByTestId('quote-from-slice'))
    const dialog = await screen.findByRole('dialog', { name: 'Quote from a slice' })
    const slots = await within(dialog).findAllByRole('combobox', { name: /^Filament for slot/ })
    expect(slots.length).toBeGreaterThan(0)
    expect(plates).toHaveBeenCalledWith(120)
    const quote = within(dialog).getByTestId('run-quote')
    expect(quote).toBeDisabled()
    for (const slot of slots) await selectOption(user, slot, /^Bambu PLA/)
    await waitFor(() => expect(quote).toBeEnabled())
    await user.click(quote)
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1))
    expect(start.mock.calls[0]?.[0].filaments).toEqual(Object.fromEntries(slots.map((_, index) => [String(index), FILAMENT_PLA])))
  })

  it('quotes an item taken from a past print from that print (#4777)', async () => {
    const added = await api.addOrderItem(ORDER_DRAFT, { archive_id: 9101, quantity: 1 })
    const onChanged = vi.fn()
    const quoted = vi.spyOn(api, 'quoteItemFromArchive')
    const { user } = renderPage(
      <QuoteEditor orderId={ORDER_DRAFT} itemId={added.id} currency="USD" exponent={2} quotes={[]} editable onChanged={onChanged} item={added} slice={{ start: vi.fn(), dismiss: vi.fn() }} />,
    )
    await user.click(screen.getByTestId('quote-from-print'))
    const form = screen.getByRole('group', { name: 'Quote from the print' })
    const quote = within(form).getByRole('button', { name: 'Quote' })
    for (const slot of within(form).queryAllByRole('combobox', { name: /^Filament for slot/ })) {
      if (quote.hasAttribute('disabled')) await selectOption(user, slot, /^Bambu PLA/)
    }
    await waitFor(() => expect(quote).toBeEnabled())
    await user.click(quote)
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(quoted).toHaveBeenCalledWith(ORDER_DRAFT, added.id, { filaments: Object.fromEntries(added.colours.map((c) => [String(c.slot), c.filament_id ?? FILAMENT_PLA])) })
  })

  it('offers no quote from a print to an item from elsewhere (#4777)', () => {
    renderPage(
      <QuoteEditor orderId={ORDER_DRAFT} itemId={ITEM_DRAFT} currency="USD" exponent={2} quotes={[]} editable onChanged={vi.fn()} item={item(ITEM_DRAFT, ORDER_DRAFT, 'draft')} slice={{ start: vi.fn(), dismiss: vi.fn() }} />,
    )
    expect(screen.queryByTestId('quote-from-print')).toBeNull()
  })

  it('keeps the dialog and its choices open when the check refuses the quote (#4693)', async () => {
    const start = vi.fn<(body: SliceQuoteRequest) => Promise<void>>().mockRejectedValue(new ApiError(409, 'The order is not open for quotes.'))
    const draft = item(ITEM_DRAFT, ORDER_DRAFT, 'draft')
    const { user } = renderPage(
      <QuoteEditor orderId={ORDER_DRAFT} itemId={ITEM_DRAFT} currency="USD" exponent={2} quotes={[]} editable onChanged={vi.fn()} item={draft} slice={{ start, dismiss: vi.fn() }} />,
    )
    await user.click(screen.getByTestId('quote-from-slice'))
    const dialog = await screen.findByRole('dialog', { name: 'Quote from a slice' })
    const slots = await within(dialog).findAllByRole('combobox', { name: /^Filament for slot/ })
    for (const slot of slots) await selectOption(user, slot, /^Bambu PLA/)
    const quote = within(dialog).getByTestId('run-quote')
    await waitFor(() => expect(quote).toBeEnabled())
    await user.click(quote)
    expect(await within(dialog).findByTestId('quote-error')).toHaveTextContent('The order is not open for quotes.')
    expect(screen.getByRole('dialog', { name: 'Quote from a slice' })).toBeInTheDocument()
  })

  it('offers to stop waiting on a quote the page no longer follows (#4693)', async () => {
    const dismiss = vi.fn()
    const draft = item(ITEM_DRAFT, ORDER_DRAFT, 'draft')
    const { user } = renderPage(
      <QuoteEditor
        orderId={ORDER_DRAFT}
        itemId={ITEM_DRAFT}
        currency="USD"
        exponent={2}
        quotes={[]}
        editable
        onChanged={vi.fn()}
        item={draft}
        slice={{ run: { startedAt: Date.now(), error: null, detached: true, known: [] }, start: vi.fn(), dismiss }}
      />,
    )
    expect(screen.getByTestId('quote-running')).toHaveTextContent(/may still be slicing/)
    await user.click(screen.getByRole('button', { name: 'Stop waiting' }))
    expect(dismiss).toHaveBeenCalledTimes(1)
  })

  it('shows a running slice quote and holds its button (#4693)', () => {
    const draft = item(ITEM_DRAFT, ORDER_DRAFT, 'draft')
    renderPage(
      <QuoteEditor
        orderId={ORDER_DRAFT}
        itemId={ITEM_DRAFT}
        currency="USD"
        exponent={2}
        quotes={[]}
        editable
        onChanged={vi.fn()}
        item={draft}
        slice={{ run: { startedAt: Date.now(), error: null, known: [] }, start: vi.fn(), dismiss: vi.fn() }}
      />,
    )
    expect(screen.getByTestId('quote-running')).toHaveTextContent(/Slicing every plate/)
    expect(screen.getByTestId('quote-from-slice')).toBeDisabled()
  })

  it('shows a failed slice quote, and offers another and a dismiss (#4693)', async () => {
    const dismiss = vi.fn()
    const draft = item(ITEM_DRAFT, ORDER_DRAFT, 'draft')
    const { user } = renderPage(
      <QuoteEditor
        orderId={ORDER_DRAFT}
        itemId={ITEM_DRAFT}
        currency="USD"
        exponent={2}
        quotes={[]}
        editable
        onChanged={vi.fn()}
        item={draft}
        slice={{ run: { startedAt: Date.now(), error: 'No printer.', known: [] }, start: vi.fn(), dismiss }}
      />,
    )
    expect(screen.getByTestId('quote-error')).toHaveTextContent('No printer.')
    expect(screen.getByTestId('quote-from-slice')).toBeEnabled()
    await user.click(within(screen.getByTestId('quote-error')).getByRole('button', { name: 'Dismiss' }))
    expect(dismiss).toHaveBeenCalledTimes(1)
  })

  it('shows a slice quote’s lines and the digest of its file', () => {
    const draft = item(ITEM_DRAFT, ORDER_DRAFT, 'quoted')
    renderPage(
      <QuoteEditor
        orderId={ORDER_DRAFT}
        itemId={ITEM_DRAFT}
        currency="USD"
        exponent={2}
        editable
        onChanged={vi.fn()}
        item={draft}
        quotes={[
          {
            id: 'q-slice',
            order_item_id: ITEM_DRAFT,
            created_at: '2026-10-22T10:00:00Z',
            source: 'slice',
            accepted: false,
            sliced_sha256: 'abcdef0123456789'.repeat(4),
            sliced_blob_key: 'quote-x',
            plates: [],
            lines: [
              { kind: 'filament', label: 'PLA Basic, 12.5 g at 25.5 per kg', quantity: 1, unit_minor: 32, amount_minor: 32 },
              { kind: 'machine_time', label: 'Machine time, 1 h at 1.5 per hour', quantity: 1, unit_minor: 150, amount_minor: 150 },
              { kind: 'rounding', label: 'Rounded up to a whole unit', quantity: 1, unit_minor: 18, amount_minor: 18 },
            ],
            unit_cost_minor: 182,
            unit_price_minor: 200,
            quantity: 1,
            total_minor: 200,
            created_by: { kind: 'browser', principal: null, session: null },
          },
        ]}
      />,
    )
    const lines = screen.getByRole('list', { name: 'Quote lines' })
    expect(within(lines).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Filament PLA Basic, 12.5 g at 25.5 per kg$0.32',
      'Machine time Machine time, 1 h at 1.5 per hour$1.50',
      'Rounding Rounded up to a whole unit$0.18',
    ])
    expect(screen.getByText(/abcdef012345/)).toBeInTheDocument()
  })

  // #4625 §6.3 — a slot that prints only support is priced with its own filament.
  it('sends a support slot with its filament, and no slot without one', async () => {
    const start = vi.fn<(body: SliceQuoteRequest) => Promise<void>>().mockResolvedValue(undefined)
    const draft = item(ITEM_DRAFT, ORDER_DRAFT, 'draft')
    const { user } = renderPage(
      <QuoteEditor orderId={ORDER_DRAFT} itemId={ITEM_DRAFT} currency="USD" exponent={2} quotes={[]} editable onChanged={vi.fn()} item={draft} slice={{ start, dismiss: vi.fn() }} />,
    )
    await user.click(screen.getByTestId('quote-from-slice'))
    const dialog = await screen.findByRole('dialog', { name: 'Quote from a slice' })
    const slots = await within(dialog).findAllByRole('combobox', { name: /^Filament for slot/ })
    for (const slot of slots) await selectOption(user, slot, /^Bambu PLA/)
    const support = within(dialog).getByRole('group', { name: 'Support filaments' })
    await user.type(within(support).getByLabelText('Support slot'), '1')
    await user.click(within(support).getByRole('button', { name: 'Add support filament' }))
    expect(within(support).getByRole('alert')).toHaveTextContent('Slot 1 is a colour slot')
    await user.clear(within(support).getByLabelText('Support slot'))
    await user.type(within(support).getByLabelText('Support slot'), '6')
    await user.click(within(support).getByRole('button', { name: 'Add support filament' }))
    await user.type(within(support).getByLabelText('Support slot'), '7')
    await user.click(within(support).getByRole('button', { name: 'Add support filament' }))
    await selectOption(user, within(support).getByRole('combobox', { name: 'Support filament for slot 6' }), /^Bambu PETG/)
    const quote = within(dialog).getByTestId('run-quote')
    await waitFor(() => expect(quote).toBeEnabled())
    await user.click(quote)
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1))
    // Slot 7 has no filament chosen: it prices nothing, so it is not sent.
    expect(start.mock.calls[0]?.[0].support_filaments).toEqual({ '5': FILAMENT_PETG })
  })

  // #4625 §6.2 — the rate a slice quote was priced at, kept with it, explains its machine time.
  it('shows the machine rate a slice quote was priced at, and its parts', async () => {
    const draft = item(ITEM_DRAFT, ORDER_DRAFT, 'quoted')
    const { user } = renderPage(
      <QuoteEditor
        orderId={ORDER_DRAFT}
        itemId={ITEM_DRAFT}
        currency="USD"
        exponent={2}
        editable
        onChanged={vi.fn()}
        item={draft}
        quotes={[
          {
            id: 'q-rate',
            order_item_id: ITEM_DRAFT,
            created_at: '2026-10-22T10:00:00Z',
            source: 'slice',
            accepted: false,
            sliced_sha256: null,
            sliced_blob_key: null,
            plates: [],
            lines: [{ kind: 'machine_time', label: 'Machine time, 1 h at 0.665 per hour', quantity: 1, unit_minor: 67, amount_minor: 67 }],
            unit_cost_minor: 67,
            unit_price_minor: 67,
            quantity: 1,
            total_minor: 67,
            created_by: { kind: 'browser', principal: null, session: null },
            printer: 'p-1',
            machine_rate_breakdown: {
              printer: 'p-1',
              printer_name: 'Garage',
              source: 'profile',
              rate_per_hour: '0.665',
              currency: 'USD',
              intervals: 'stored',
              purchase_price: '2400',
              watts: '200',
              kwh_price: '0.15',
              depreciation_per_hour: '0.48',
              power_per_hour: '0.03',
              wear_per_hour: '0.035',
              maintenance_per_hour: '0.12',
            },
          },
        ]}
      />,
    )
    const rate = screen.getByTestId('quote-machine-rate')
    expect(rate).toHaveTextContent('Machine rate 0.665 per hour for Garage, with the intervals last read: Bambuddy did not answer')
    await user.click(within(rate).getByRole('button', { name: /Machine rate 0.665/ }))
    expect(within(rate).getByRole('list', { name: 'Machine rate parts' })).toHaveTextContent('Depreciation 0.48 per hour')
  })
})

// #5328, job cost spec §6.3: the quote warning, advice only, and one click to its price.
describe('QuoteEditor · the quote warning (#5328)', () => {
  beforeEach(() => {
    resetMockState()
    // 9.00 a unit over 4 jobs, failed prints the main reason; no markup, thin below 15 %.
    setMockSettings({ ...mockSettings(), pricing_markup_percent: '0', orders_thin_margin_percent: '15' })
    seedUnitCost('name-keychain', { jobs: 4, units: 6, unit_cost_minor: 900, estimated: false, main_reason: 'failed_prints', main_reason_minor: 250 })
  })

  function editor(quotes: Quote[] = []) {
    const draft = item(ITEM_DRAFT, ORDER_DRAFT, 'draft')
    return renderPage(
      <QuoteEditor
        orderId={ORDER_DRAFT}
        itemId={ITEM_DRAFT}
        currency="USD"
        exponent={2}
        quotes={quotes}
        editable
        onChanged={vi.fn()}
        item={draft}
        slice={{ start: vi.fn(), dismiss: vi.fn() }}
      />,
    )
  }

  it('warns on a typed price below the real cost, and one click uses the suggested price', async () => {
    const { user } = editor()
    const field = screen.getByLabelText('Price per unit (USD)')
    await user.type(field, '8')
    const warning = await screen.findByTestId('quote-warning')
    expect(warning).toHaveTextContent("$8.00 a unit is below this model's real cost: $9.00 a unit over its last 4 jobs.")
    expect(warning).toHaveTextContent('Main reason: failed prints, $2.50 a unit.')
    // 9.00 / 0.85 = 10.588…, up to 10.59: the lowest price that is not thin.
    await user.click(within(warning).getByRole('button', { name: 'Use $10.59' }))
    expect(field).toHaveValue('10.59')
    expect(screen.queryByTestId('quote-warning')).toBeNull()
    // Advice only: the quote can still be recorded.
    expect(screen.getByRole('button', { name: 'Record only' })).toBeEnabled()
  })

  // A slice quote not yet accepted, at 6.00 a unit: below the 9.00 real cost.
  const lowSlice = (): Quote => ({
    id: 'q-low',
    order_item_id: ITEM_DRAFT,
    created_at: '2026-11-05T10:00:00Z',
    source: 'slice',
    accepted: false,
    sliced_sha256: null,
    sliced_blob_key: null,
    plates: [],
    lines: [],
    unit_cost_minor: 500,
    unit_price_minor: 600,
    quantity: 1,
    total_minor: 600,
    created_by: { kind: 'browser', principal: null, session: null },
  })

  it("reads the history again when the item's parts change (#5549 review)", async () => {
    const draft = item(ITEM_DRAFT, ORDER_DRAFT, 'draft')
    const view = (parts: string) => (
      <QuoteEditor orderId={ORDER_DRAFT} itemId={ITEM_DRAFT} currency="USD" exponent={2} quotes={[]} editable onChanged={vi.fn()} item={draft} parts={parts} slice={{ start: vi.fn(), dismiss: vi.fn() }} />
    )
    const { user, rerender } = renderPage(view(''))
    await user.type(screen.getByLabelText('Price per unit (USD)'), '8')
    expect(await screen.findByTestId('quote-warning')).toHaveTextContent('real cost: $9.00 a unit')
    // Two hooks saved in the Parts editor: the page passes the item's new parts.
    seedUnitCost('name-keychain', { jobs: 4, units: 6, unit_cost_minor: 1100, estimated: false, main_reason: 'other', main_reason_minor: 200 })
    rerender(view('hook:2'))
    await waitFor(() => expect(screen.getByTestId('quote-warning')).toHaveTextContent('real cost: $11.00 a unit'))
  })

  it('warns on a slice quote not yet accepted, and says nothing on a price that is not thin', async () => {
    const { user } = editor([lowSlice()])
    expect(await screen.findByTestId('quote-warning')).toHaveTextContent('$6.00 a unit is below')
    await user.type(screen.getByLabelText('Price per unit (USD)'), '12')
    await waitFor(() => expect(screen.queryByTestId('quote-warning')).toBeNull())
  })

  it('warns about nothing while the typed price does not parse, and about the pending quote once the field is empty (#5533)', async () => {
    const { user } = editor([lowSlice()])
    const field = screen.getByLabelText('Price per unit (USD)')
    expect(await screen.findByTestId('quote-warning')).toHaveTextContent('$6.00 a unit is below')
    await user.type(field, 'abc')
    await waitFor(() => expect(screen.queryByTestId('quote-warning')).toBeNull())
    // The pending quote's answer does not come back while the field holds 'abc'.
    await settled()
    expect(screen.queryByTestId('quote-warning')).toBeNull()
    await user.clear(field)
    expect(await screen.findByTestId('quote-warning')).toHaveTextContent('$6.00 a unit is below')
  })

  it('calls a price above the cost but under the thin threshold a thin margin, not a loss', async () => {
    const { user } = editor()
    // 10.00 over 9.00: a 10 % margin, under 15 %, and above the cost.
    await user.type(screen.getByLabelText('Price per unit (USD)'), '10')
    const warning = await screen.findByTestId('quote-warning')
    expect(warning).toHaveTextContent("$10.00 a unit leaves a thin margin, below 15 %, over this model's real cost: $9.00 a unit over its last 4 jobs.")
    expect(warning).not.toHaveTextContent('is below')
  })

  it('follows the route on the thin boundary and sends it the price', async () => {
    const { user } = editor()
    const field = screen.getByLabelText('Price per unit (USD)')
    // 9.00 / 0.85 = 10.588…: 10.58 is a 14.9 % margin, thin; 10.59 is 15.0 %, not thin.
    await user.type(field, '10.58')
    expect(await screen.findByTestId('quote-warning')).toHaveTextContent('$10.58 a unit leaves a thin margin')
    await user.clear(field)
    await user.type(field, '10.59')
    await waitFor(() => expect(screen.queryByTestId('quote-warning')).toBeNull())
    // Nothing shows for 10.59 once its own answer has landed either.
    await settled()
    expect(screen.queryByTestId('quote-warning')).toBeNull()
  })

  it('reads the history again after a quote is recorded', async () => {
    const { user } = editor()
    const field = screen.getByLabelText('Price per unit (USD)')
    await user.type(field, '8')
    // The first read has landed: 8.00 is below its 9.00.
    await screen.findByTestId('quote-warning')
    await user.clear(field)
    await user.type(field, '11')
    // 11.00 over the 9.00 read at first is an 18 % margin: no warning.
    expect(screen.queryByTestId('quote-warning')).toBeNull()
    // A job fulfilled while the editor is open: the real cost is now 12.00.
    seedUnitCost('name-keychain', { jobs: 5, units: 7, unit_cost_minor: 1200, estimated: false, main_reason: null, main_reason_minor: null })
    await user.click(screen.getByRole('button', { name: 'Record only' }))
    await waitFor(() => expect(field).toHaveValue(''))
    await user.type(field, '11')
    expect(await screen.findByTestId('quote-warning')).toHaveTextContent('$11.00 a unit is below this model')
  })

  it('shows nothing for a model with no history', async () => {
    reset()
    const { user } = editor()
    await user.type(screen.getByLabelText('Price per unit (USD)'), '1')
    await settled()
    expect(screen.queryByTestId('quote-warning')).toBeNull()
  })
})

// #5554 — a quote, a running slice and a failed one each say when, in the viewer's local
// time, with the full timestamp on hover. The cases run in New York (EST in November), so
// 14:04Z reads 09:04 whether the locale writes 12 or 24 hours.
describe('QuoteEditor · times (#5554)', () => {
  const zone = process.env.TZ
  const NOW = Date.parse('2026-11-06T14:07:00Z')
  beforeEach(() => {
    resetMockState()
    process.env.TZ = 'America/New_York'
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
  })
  afterEach(() => {
    vi.useRealTimers()
    if (zone === undefined) delete process.env.TZ
    else process.env.TZ = zone
  })

  const at = (iso: string) => Date.parse(iso)

  function quote(id: string, createdAt: string, accepted: boolean): Quote {
    return {
      id,
      order_item_id: ITEM_DRAFT,
      created_at: createdAt,
      source: 'manual',
      accepted,
      sliced_sha256: null,
      sliced_blob_key: null,
      plates: [],
      lines: [{ kind: 'manual', label: 'Quoted by hand', quantity: 1, unit_minor: 900, amount_minor: 900 }],
      unit_cost_minor: 900,
      unit_price_minor: 900,
      quantity: 1,
      total_minor: 900,
      created_by: { kind: 'browser', principal: null, session: null },
    }
  }

  function editor(quotes: Quote[], run?: SliceQuoteRun) {
    const draft = item(ITEM_DRAFT, ORDER_DRAFT, 'draft')
    return renderPage(
      <QuoteEditor
        orderId={ORDER_DRAFT}
        itemId={ITEM_DRAFT}
        currency="USD"
        exponent={2}
        quotes={quotes}
        editable
        onChanged={vi.fn()}
        item={draft}
        slice={{ run, start: vi.fn(), dismiss: vi.fn() }}
      />,
    )
  }

  it('shows when a running slice started and how long it has run', () => {
    editor([], { startedAt: at('2026-11-06T14:04:00Z'), error: null, known: [] })
    const running = screen.getByTestId('quote-running')
    expect(running).toHaveTextContent(/Started 09:04/)
    expect(screen.getByTestId('quote-elapsed')).toHaveTextContent('3m')
    const time = within(running).getByText(/09:04/)
    expect(time.tagName).toBe('TIME')
    expect(time).toHaveAttribute('dateTime', '2026-11-06T14:04:00.000Z')
    expect(time.getAttribute('title')).toMatch(/2026.*9:04:00.*(EST|GMT-5)/)
  })

  it('shows when a failed slice started and ended, with its error', () => {
    editor([], { startedAt: at('2026-11-06T14:04:00Z'), finishedAt: at('2026-11-06T14:06:00Z'), error: 'No printer.', known: [] })
    expect(screen.getByTestId('quote-error')).toHaveTextContent('The slice quote failed: No printer.')
    expect(screen.getByTestId('quote-error-times')).toHaveTextContent(/Started 09:04.*ended 09:06/)
  })

  it('shows when each quote was made, and marks the newest and the accepted one', () => {
    editor([quote('q-old', '2026-11-05T13:00:00Z', true), quote('q-new', '2026-11-06T14:00:00Z', false), quote('q-mid', '2026-11-06T13:30:00Z', false)])
    const old = screen.getByTestId('quote-q-old')
    const fresh = screen.getByTestId('quote-q-new')
    // Another day carries its date.
    expect(old).toHaveTextContent(/Quoted Nov 5, 08:00/)
    expect(old).toHaveTextContent('Accepted')
    expect(within(old).queryByTestId('quote-newest')).toBeNull()
    expect(fresh).toHaveTextContent(/Quoted 09:00/)
    expect(within(fresh).getByTestId('quote-newest')).toHaveTextContent('Newest')
    expect(screen.getAllByTestId('quote-newest')).toHaveLength(1)
  })

  it('marks no newest quote when there is only one', () => {
    editor([quote('q-only', '2026-11-06T14:00:00Z', false)])
    expect(screen.queryByTestId('quote-newest')).toBeNull()
  })
})
