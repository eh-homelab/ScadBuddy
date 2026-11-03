import { screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import type { Fulfilment } from '../../api/types'
import { resetMockState } from '../../mocks/handlers'
import { BROWSER, ITEM_APPROVED, ORDER_ACCEPTED, QUOTE_ACCEPTED, state } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { OrderCosts } from './OrderCosts'

function attempt(n: number, outcome: Fulfilment['outcome'], grams: number): void {
  state.fulfilments.push({
    id: `f-${n}`,
    order_item_id: ITEM_APPROVED,
    attempt: n,
    quantity: outcome === 'succeeded' ? 2 : 0,
    outcome,
    failure_reason: null,
    started_at: null,
    finished_at: '2026-11-01T12:00:00Z',
    notes: '',
    accepted_at: null,
    created_by: BROWSER,
    created_at: '2026-11-01T10:00:00Z',
    actual_seconds: 500,
    actual_grams: grams,
    archive_ids: [8000 + n],
  })
}

// #4251 §6.4 — the accepted quote beside the attempts, failures included.
describe('OrderCosts', () => {
  beforeEach(() => {
    resetMockState()
    const quote = state.quotes.find((q) => q.id === QUOTE_ACCEPTED)!
    Object.assign(quote, {
      source: 'slice',
      plates: [{ index: 1, print_seconds: 600, filaments: [{ slot: 0, used_g: '10' }] }],
      lines: [{ kind: 'filament', label: 'PLA, 10 g', quantity: 1, unit_minor: 20, amount_minor: 20 }],
    })
  })

  it('lays the quote beside every attempt, the failed one counted', async () => {
    attempt(1, 'failed', 7.5)
    attempt(2, 'succeeded', 21)
    renderPage(<OrderCosts orderId={ORDER_ACCEPTED} />)

    const row = await screen.findByRole('row', { name: /Name Keychain/ })
    expect(within(row).getByTestId('cost-quoted')).toHaveTextContent('20 g')
    expect(within(row).getByTestId('cost-quoted')).toHaveTextContent('$0.40')
    expect(within(row).getByTestId('cost-actual')).toHaveTextContent('28.5 g')
    expect(within(row).getByTestId('cost-actual')).toHaveTextContent('$0.57')
    expect(within(row).getByTestId('cost-failed')).toHaveTextContent('1 failed · 7.5 g · $0.15')
  })

  it('says so when nothing has printed yet', async () => {
    renderPage(<OrderCosts orderId={ORDER_ACCEPTED} />)
    expect(await screen.findByText('No attempt has used anything yet.')).toBeInTheDocument()
  })
})
