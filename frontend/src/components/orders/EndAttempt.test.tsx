import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { Fulfilment } from '../../api/types'
import { resetMockState } from '../../mocks/handlers'
import { ITEM_APPROVED, ITEM_DRAFT, ORDER_ACCEPTED, ORDER_DRAFT, revisionsOf, state } from '../../mocks/features/orders/state'
import { OrderPage } from '../../pages/OrderPage'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'

vi.mock('../print/PlateScene', () => ({ PlateScene: () => <div data-testid="plate-scene" /> }))

function render(orderId: string, itemId: string) {
  return renderPage(
    <Routes>
      <Route path="/orders/:id" element={<OrderPage />} />
    </Routes>,
    { route: `/orders/${orderId}?item=${itemId}` },
  )
}

/** A print of the item still open: its run attached, no outcome and no end yet. */
function inFlight(itemId: string, extra: Partial<Fulfilment> = {}): Fulfilment {
  const attempt: Fulfilment = {
    ...state.fulfilments[0]!,
    id: `f-open-${itemId}`,
    order_item_id: itemId,
    attempt: 1,
    quantity: 0,
    outcome: 'unknown',
    failure_reason: null,
    started_at: '2026-11-04T10:00:00Z',
    finished_at: null,
    notes: 'first go',
    accepted_at: null,
    print_run_id: 'run-open',
    copies: 2,
    purpose: 'production',
    ...extra,
  }
  state.fulfilments.push(attempt)
  return attempt
}

// #5316 — an attempt that never ends blocks every later print of its item
// (`print_in_flight`), so the item drawer ends it by hand: its outcome and end time.
describe('EndAttempt (#5316)', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it('ends a production print as failed, and the item prints again', async () => {
    inFlight(ITEM_APPROVED)
    state.items.find((i) => i.id === ITEM_APPROVED)!.status = 'queued'
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.status = 'in_production'
    const updated = vi.spyOn(api, 'updateFulfilment')
    const { user } = render(ORDER_ACCEPTED, ITEM_APPROVED)
    const attempts = await screen.findByRole('list', { name: 'Attempts' })
    expect(screen.queryByTestId('print-item')).toBeNull()
    await user.click(within(attempts).getByRole('button', { name: 'End attempt 1' }))
    const form = await screen.findByRole('form', { name: 'End attempt 1' })
    await choose(user, within(form).getByRole('combobox', { name: 'Outcome' }), 'failed')
    await user.type(within(form).getByLabelText('Why it failed'), 'spaghetti')
    const made = within(form).getByLabelText('Made')
    await user.clear(made)
    await user.type(made, '0')
    await user.click(within(form).getByRole('button', { name: 'Save outcome' }))
    await waitFor(() => expect(updated).toHaveBeenCalledTimes(1))
    const [orderId, itemId, fulfilmentId, body] = updated.mock.calls[0]!
    expect([orderId, itemId, fulfilmentId]).toEqual([ORDER_ACCEPTED, ITEM_APPROVED, `f-open-${ITEM_APPROVED}`])
    // The PATCH re-reads the attempt whole: what it had is sent again.
    expect(body).toMatchObject({ quantity: 0, outcome: 'failed', failure_reason: 'spaghetti', started_at: '2026-11-04T10:00:00Z', notes: 'first go' })
    expect(body.finished_at).toEqual(expect.any(String))
    expect(Number.isNaN(Date.parse(body.finished_at!))).toBe(false)
    const attempt = state.fulfilments.find((f) => f.id === `f-open-${ITEM_APPROVED}`)!
    expect(attempt).toMatchObject({ outcome: 'failed', failure_reason: 'spaghetti' })
    expect(attempt.finished_at).not.toBeNull()
    expect(state.items.find((i) => i.id === ITEM_APPROVED)!.status).toBe('failed')
    expect(await screen.findByTestId('print-item')).toHaveTextContent('Print again…')
    expect(screen.queryByRole('button', { name: 'End attempt 1' })).toBeNull()
  })

  it('ends a production print as succeeded with the copies made by default', async () => {
    inFlight(ITEM_APPROVED)
    state.items.find((i) => i.id === ITEM_APPROVED)!.status = 'printing'
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.status = 'in_production'
    const { user } = render(ORDER_ACCEPTED, ITEM_APPROVED)
    await user.click(await screen.findByRole('button', { name: 'End attempt 1' }))
    const form = await screen.findByRole('form', { name: 'End attempt 1' })
    expect(within(form).getByLabelText('Made')).toHaveValue('2')
    expect(within(form).queryByLabelText('Why it failed')).toBeNull()
    await user.click(within(form).getByRole('button', { name: 'Save outcome' }))
    await waitFor(() => expect(state.fulfilments.find((f) => f.id === `f-open-${ITEM_APPROVED}`)!.outcome).toBe('succeeded'))
    expect(state.fulfilments.find((f) => f.id === `f-open-${ITEM_APPROVED}`)!.quantity).toBe(2)
    expect(state.items.find((i) => i.id === ITEM_APPROVED)!.status).toBe('printed')
    expect(await screen.findByRole('button', { name: 'Accept attempt 1' })).toBeInTheDocument()
  })

  it("ends a test print on its revision and leaves the item as it was", async () => {
    const first = revisionsOf(ITEM_DRAFT)[0]!
    inFlight(ITEM_DRAFT, { purpose: 'test', revision_id: first.id, copies: 1 })
    const updated = vi.spyOn(api, 'updateFulfilment')
    const { user } = render(ORDER_DRAFT, ITEM_DRAFT)
    const tests = await screen.findByRole('list', { name: `Test prints of revision ${first.number}` })
    await user.click(within(tests).getByRole('button', { name: 'End attempt 1' }))
    const form = await screen.findByRole('form', { name: 'End attempt 1' })
    // A test print makes nothing of the item.
    expect(within(form).queryByLabelText('Made')).toBeNull()
    await choose(user, within(form).getByRole('combobox', { name: 'Outcome' }), 'partial')
    await user.click(within(form).getByRole('button', { name: 'Save outcome' }))
    await waitFor(() => expect(updated).toHaveBeenCalledTimes(1))
    expect(updated.mock.calls[0]![3]).toMatchObject({ quantity: 0, outcome: 'partial' })
    const attempt = state.fulfilments.find((f) => f.id === `f-open-${ITEM_DRAFT}`)!
    expect(attempt.outcome).toBe('partial')
    expect(attempt.finished_at).not.toBeNull()
    expect(state.items.find((i) => i.id === ITEM_DRAFT)!.status).toBe('draft')
    expect(await within(tests).findByText('Test print, attempt 1: partial')).toBeInTheDocument()
  })

  it("shows the server's refusal and keeps the form", async () => {
    inFlight(ITEM_APPROVED)
    state.items.find((i) => i.id === ITEM_APPROVED)!.status = 'queued'
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.status = 'in_production'
    const { user } = render(ORDER_ACCEPTED, ITEM_APPROVED)
    await user.click(await screen.findByRole('button', { name: 'End attempt 1' }))
    const form = await screen.findByRole('form', { name: 'End attempt 1' })
    state.fulfilments.find((f) => f.id === `f-open-${ITEM_APPROVED}`)!.accepted_at = '2026-11-04T12:00:00Z'
    await user.click(within(form).getByRole('button', { name: 'Save outcome' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('is accepted')
  })

  it('offers no end for an attempt already ended or accepted', async () => {
    inFlight(ITEM_APPROVED, { finished_at: '2026-11-04T11:00:00Z' })
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.status = 'in_production'
    render(ORDER_ACCEPTED, ITEM_APPROVED)
    await screen.findByRole('list', { name: 'Attempts' })
    expect(screen.queryByRole('button', { name: /^End attempt/ })).toBeNull()
  })
})
