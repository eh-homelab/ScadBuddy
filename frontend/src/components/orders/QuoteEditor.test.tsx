import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { FILAMENT_PETG, FILAMENT_PLA, ITEM_DRAFT, ORDER_DRAFT, item } from '../../mocks/features/orders/state'
import { selectOption } from '../../test/select'
import { renderPage } from '../../test/utils'
import { QuoteEditor } from './QuoteEditor'

vi.mock('../print/PlateScene', () => ({ PlateScene: () => <div data-testid="plate-scene" /> }))

// #4250 — "Quote from a slice…" opens the print dialog in its quote mode: the spools slice,
// a price-book filament per slot prices, and Quote posts the item's slice quote.
describe('QuoteEditor · a quote from a slice (#4250)', () => {
  beforeEach(() => resetMockState())

  it('quotes with the dialog’s choices and a filament per slot', async () => {
    const quoted = vi.spyOn(api, 'quoteItem')
    const onChanged = vi.fn()
    const draft = item(ITEM_DRAFT, ORDER_DRAFT, 'draft', { quantity: 2 })
    const { user } = renderPage(
      <QuoteEditor orderId={ORDER_DRAFT} itemId={ITEM_DRAFT} currency="USD" exponent={2} quotes={[]} editable onChanged={onChanged} item={draft} />,
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
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    const body = quoted.mock.calls[0]?.[2]
    expect(body?.filaments).toEqual(
      Object.fromEntries(slots.map((_, index) => [String(index), index % 2 === 0 ? FILAMENT_PLA : FILAMENT_PETG])),
    )
    expect(body?.filament_plan.slots?.length).toBeGreaterThan(0)
    expect(body).not.toHaveProperty('project_id')
    quoted.mockRestore()
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
