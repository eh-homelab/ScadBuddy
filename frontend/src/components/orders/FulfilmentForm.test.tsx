import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { ITEM_APPROVED, ORDER_ACCEPTED, state } from '../../mocks/features/orders/state'
import { OrderPage } from '../../pages/OrderPage'
import { renderPage } from '../../test/utils'

vi.mock('../print/PlateScene', () => ({ PlateScene: () => <div data-testid="plate-scene" /> }))

function render() {
  return renderPage(
    <Routes>
      <Route path="/orders/:id" element={<OrderPage />} />
    </Routes>,
    { route: `/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}` },
  )
}

describe('FulfilmentForm stale attempts (#5592)', () => {
  beforeEach(() => resetMockState())

  it('marks an old design attempt and offers no Accept for it', async () => {
    const old = await api.recordFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 1, outcome: 'succeeded' })
    // It names revision 1. Its time is moved past the revision that follows, as the backend's
    // test does: the revision's number orders them, not the clock (#5704).
    state.fulfilments.find((f) => f.id === old.id)!.created_at = '2099-01-01T00:00:00Z'
    await api.addOrderItemRevisionAfterAcceptance(ORDER_ACCEPTED, ITEM_APPROVED, { note: 'loop' })
    const quote = await api.quoteOrderItem(ORDER_ACCEPTED, ITEM_APPROVED, { unit_price_minor: 1300 })
    await api.acceptOrderQuote(ORDER_ACCEPTED, ITEM_APPROVED, quote.id)
    const fresh = await api.recordFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 1, outcome: 'succeeded' })

    render()
    const list = await screen.findByRole('list', { name: 'Attempts' })
    const [oldRow, freshRow] = within(list).getAllByRole('listitem')
    expect(oldRow).toHaveTextContent(`Attempt ${old.attempt}`)
    expect(within(oldRow!).getByText('Old design')).toBeInTheDocument()
    expect(within(oldRow!).queryByRole('button', { name: `Accept attempt ${old.attempt}` })).toBeNull()
    expect(within(freshRow!).queryByText('Old design')).toBeNull()
    expect(within(freshRow!).getByRole('button', { name: `Accept attempt ${fresh.attempt}` })).toBeInTheDocument()
  })

  it('offers Accept for an attempt of the priced design', async () => {
    const attempt = await api.recordFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 1, outcome: 'succeeded' })
    render()
    const list = await screen.findByRole('list', { name: 'Attempts' })
    expect(within(list).getByRole('button', { name: `Accept attempt ${attempt.attempt}` })).toBeInTheDocument()
    expect(within(list).queryByText('Old design')).toBeNull()
  })
})

// Spec 2026-11-05 §4.4: no order event reaches the page (F13), so an attempt by hand that
// names an archive shows its archive row as soon as the page lists it, with no click.
describe('FulfilmentForm archive row (#7236)', () => {
  beforeEach(() => resetMockState())

  it("loads a hand attempt's archive row with no click", async () => {
    const attempt = await api.recordFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 1, outcome: 'succeeded', archive_id: 81 })
    render()
    const archives = await screen.findByLabelText(`Attempt ${attempt.attempt}'s Bambuddy archives`)
    await waitFor(() => expect(archives).toHaveTextContent('Archive 81'))
    // Bambuddy finished the archive just now: its timelapse is not processed yet.
    expect(archives).toHaveTextContent('waiting for Bambuddy to process the timelapse')
  })

  it('reads the row again when the attempt names another archive', async () => {
    const attempt = await api.recordFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 1, outcome: 'succeeded', archive_id: 81 })
    const { user } = render()
    const archives = await screen.findByLabelText(`Attempt ${attempt.attempt}'s Bambuddy archives`)
    await waitFor(() => expect(archives).toHaveTextContent('Archive 81'))
    state.fulfilments.find((f) => f.id === attempt.id)!.archive_ids = [82]
    // Any change on the page reads the order again; here, an accept.
    await user.click(screen.getByRole('button', { name: `Accept attempt ${attempt.attempt}` }))
    const again = await screen.findByLabelText(`Attempt ${attempt.attempt}'s Bambuddy archives`)
    await waitFor(() => expect(again).toHaveTextContent('Archive 82'))
  })
})
