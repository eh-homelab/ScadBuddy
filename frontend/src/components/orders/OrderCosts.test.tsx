import { render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import type { Fulfilment } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { resetMockState } from '../../mocks/handlers'
import { BROWSER, ITEM_APPROVED, ORDER_ACCEPTED, QUOTE_ACCEPTED, state } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { timerKey } from './CostEntryForms'
import { MarginBadge, OrderCosts } from './OrderCosts'

function attempt(n: number, outcome: Fulfilment['outcome'], grams: number): void {
  state.fulfilments.push({
    id: `f-${n}`,
    order_item_id: ITEM_APPROVED,
    purpose: 'production',
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

/** The card as the order page mounts it: over the costs read, reloaded after a write. */
function Card() {
  const view = useAsync(() => api.orderCosts(ORDER_ACCEPTED), [])
  return view.data ? <OrderCosts costs={view.data} onChanged={view.refresh} /> : null
}

// #4251 §6.4, #5326 §6.1 — the money view: charged, real cost and margin per item.
describe('OrderCosts', () => {
  beforeEach(() => {
    resetMockState()
    window.localStorage.clear()
    const quote = state.quotes.find((q) => q.id === QUOTE_ACCEPTED)!
    Object.assign(quote, {
      source: 'slice',
      plates: [{ index: 1, print_seconds: 600, filaments: [{ slot: 0, used_g: '10' }] }],
      lines: [{ kind: 'filament', label: 'PLA, 10 g', quantity: 1, unit_minor: 20, amount_minor: 20 }],
    })
  })

  it('shows charged, real cost and margin, and each attempt when opened', async () => {
    attempt(1, 'failed', 7.5)
    attempt(2, 'succeeded', 21)
    const { user } = renderPage(<Card />)

    const row = await screen.findByRole('row', { name: /Name Keychain/ })
    expect(within(row).getByTestId('cost-charged')).toHaveTextContent('$30.00')
    // 28.5 g at the quote's 2 cents a gram; today's machine rate and labour are 0.
    expect(within(row).getByTestId('cost-real')).toHaveTextContent('≈ $0.57')
    expect(within(row).getByTestId('margin')).toHaveTextContent('$29.43 (98.1%)')
    expect(row).toHaveAttribute('data-state', 'ok')

    await user.click(within(row).getByRole('button', { name: /Name Keychain/ }))
    const attempts = await screen.findByRole('list', { name: 'Attempts' })
    expect(within(attempts).getByText(/Attempt 1 \(production, failed\): 7.5 g · 8m · filament \$0.15 · machine \$0.00 · estimated machine rate/)).toBeInTheDocument()
    // The order adds its shipping charge to what was charged.
    expect(within(screen.getByRole('row', { name: 'Order total' })).getByText('$35.00')).toBeInTheDocument()
  })

  it('tints a loss and logs time at the labour rate', async () => {
    await api.putSettings({ pricing_labour_rate_per_hour: '600' })
    const { user } = renderPage(<Card />)
    const row = await screen.findByRole('row', { name: /Name Keychain/ })
    await user.click(within(row).getByRole('button', { name: /Name Keychain/ }))

    const form = await screen.findByRole('form', { name: 'Log time on this item' })
    await user.type(within(form).getByLabelText('Minutes'), '45')
    await user.click(within(form).getByRole('button', { name: 'Log time' }))

    // 45 minutes at 600 an hour is 450.00, over the 30.00 charged.
    await waitFor(() => expect(screen.getByRole('row', { name: /Name Keychain/ })).toHaveAttribute('data-state', 'loss'))
    expect(state.timeEntries.map((e) => [e.order_item_id, e.minutes, e.rate_minor_per_hour])).toEqual([[ITEM_APPROVED, 45, 60000]])
    expect(await screen.findByText(/Setup, 45 min · \$450.00/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Remove Setup time, 45 min' }))
    await waitFor(() => expect(state.timeEntries).toEqual([]))
  })

  it('adds a cost to the order', async () => {
    const { user } = renderPage(<Card />)
    const form = await screen.findByRole('form', { name: 'Add a cost to the order' })
    await user.type(within(form).getByLabelText('Amount (USD)'), '1.25')
    await user.type(within(form).getByLabelText('Note'), 'magnets')
    await user.click(within(form).getByRole('button', { name: 'Add cost' }))

    await waitFor(() => expect(state.costEntries.map((e) => [e.order_item_id, e.kind, e.amount_minor, e.note])).toEqual([[null, 'material', 125, 'magnets']]))
    expect(await screen.findByText('Materials, $1.25 · magnets')).toBeInTheDocument()
  })

  it('stops a timer started earlier into one entry, rounded up to the minute', async () => {
    const started = new Date(Date.now() - 9.5 * 60_000).toISOString()
    window.localStorage.setItem(timerKey(ITEM_APPROVED), started)
    const { user } = renderPage(<Card />)
    const row = await screen.findByRole('row', { name: /Name Keychain/ })
    await user.click(within(row).getByRole('button', { name: /Name Keychain/ }))

    await user.click(await screen.findByRole('button', { name: 'Stop timer (10 min)' }))

    await waitFor(() => expect(state.timeEntries.map((e) => e.minutes)).toEqual([10]))
    expect(state.timeEntries[0]!.started_at).not.toBeNull()
    // Its scope and start name it, so a retry after a lost reply writes once.
    expect(state.timeEntries[0]!.source).toBe(`timer:${ITEM_APPROVED}:${started}`)
    expect(window.localStorage.getItem(timerKey(ITEM_APPROVED))).toBeNull()
    expect(await screen.findAllByRole('button', { name: 'Start timer' })).not.toHaveLength(0)
  })

  it('marks a collapsed row whose timer runs, and says the timer is on this browser only', async () => {
    window.localStorage.setItem(timerKey(ITEM_APPROVED), new Date(Date.now() - 60_000).toISOString())
    const { user } = renderPage(<Card />)
    const row = await screen.findByRole('row', { name: /Name Keychain/ })
    expect(within(row).getByText('Timer running')).toBeInTheDocument()

    await user.click(within(row).getByRole('button', { name: /Name Keychain/ }))

    expect(await screen.findByText('on this browser only')).toBeInTheDocument()
    expect(within(row).queryByText('Timer running')).toBeNull()
  })

  it('offers to log or discard a timer older than 12 hours', async () => {
    window.localStorage.setItem(timerKey(ITEM_APPROVED), new Date(Date.now() - 13 * 60 * 60_000).toISOString())
    const { user } = renderPage(<Card />)
    const row = await screen.findByRole('row', { name: /Name Keychain/ })
    await user.click(within(row).getByRole('button', { name: /Name Keychain/ }))

    const offer = await screen.findByRole('status')
    expect(offer).toHaveTextContent(/This timer has run since/)
    await user.click(within(offer).getByRole('button', { name: 'Discard timer' }))

    expect(window.localStorage.getItem(timerKey(ITEM_APPROVED))).toBeNull()
    expect(state.timeEntries).toEqual([])
    expect(screen.queryByRole('status')).toBeNull()
  })
})

describe('MarginBadge', () => {
  it('says Loss or Thin margin, and nothing otherwise', () => {
    const { rerender } = render(<MarginBadge state="loss" />)
    expect(screen.getByText('Loss')).toBeInTheDocument()
    rerender(<MarginBadge state="thin" />)
    expect(screen.getByText('Thin margin')).toBeInTheDocument()
    rerender(<MarginBadge state="ok" />)
    expect(screen.queryByText(/Loss|Thin/)).toBeNull()
  })
})
