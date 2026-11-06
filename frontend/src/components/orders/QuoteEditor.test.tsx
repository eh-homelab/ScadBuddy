import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { Quote, SliceQuoteRequest } from '../../api/types'
import { reset, seedUnitCost } from '../../mocks/features/orders/history'
import { printersState } from '../../mocks/features/printers'
import { mockSettings, resetMockState, setMockSettings } from '../../mocks/handlers'
import { FILAMENT_PETG, FILAMENT_PLA, ITEM_DRAFT, ORDER_DRAFT, item } from '../../mocks/features/orders/state'
import { selectOption } from '../../test/select'
import { renderPage } from '../../test/utils'
import { QuoteEditor } from './QuoteEditor'

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

  it('warns on a slice quote not yet accepted, and says nothing on a price that is not thin', async () => {
    const slice: Quote = {
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
    }
    const { user } = editor([slice])
    expect(await screen.findByTestId('quote-warning')).toHaveTextContent('$6.00 a unit is below')
    await user.type(screen.getByLabelText('Price per unit (USD)'), '12')
    await waitFor(() => expect(screen.queryByTestId('quote-warning')).toBeNull())
  })

  it('shows nothing for a model with no history', async () => {
    reset()
    const { user } = editor()
    await user.type(screen.getByLabelText('Price per unit (USD)'), '1')
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.queryByTestId('quote-warning')).toBeNull()
  })
})
