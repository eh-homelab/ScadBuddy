import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Fulfilment } from '../../api/types'
import { resetMockState } from '../../mocks/handlers'
import { BROWSER, ITEM_APPROVED, ORDER_ACCEPTED, state } from '../../mocks/features/orders/state'
import { OrderPage } from '../../pages/OrderPage'
import { renderPage } from '../../test/utils'

vi.mock('../print/PlateScene', () => ({ PlateScene: () => <div data-testid="plate-scene" /> }))

/** An attempt whose print settled: what the print path records. */
function printedAttempt(): Fulfilment {
  const attempt: Fulfilment = {
    id: 'f-printed',
    order_item_id: ITEM_APPROVED,
    purpose: 'production',
    attempt: 1,
    quantity: 2,
    outcome: 'succeeded',
    failure_reason: null,
    started_at: '2026-11-01T10:00:00Z',
    finished_at: '2026-11-01T12:00:00Z',
    notes: '',
    accepted_at: null,
    created_by: BROWSER,
    created_at: '2026-11-01T10:00:00Z',
    print_run_id: 'run-printed',
    copies: 2,
  }
  state.fulfilments.push(attempt)
  return attempt
}

// #4253 §8.2 — a printed attempt offers its Bambuddy archive's finish photo and timelapse
// as one-click copies into the item's files.
describe('ArchiveCopies', () => {
  beforeEach(() => resetMockState())

  it("copies the archive's finish photo and timelapse into the item's files, once each", async () => {
    printedAttempt()
    const { user } = renderPage(
      <Routes>
        <Route path="/orders/:id" element={<OrderPage />} />
      </Routes>,
      { route: `/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}` },
    )
    const attempts = await screen.findByRole('list', { name: 'Attempts' })
    await user.click(within(attempts).getByRole('button', { name: 'From Bambuddy…' }))
    const archives = await screen.findByLabelText("Attempt 1's Bambuddy archives")
    expect(archives).toHaveTextContent('Archive 8001')
    // Reading the archives copied what they say onto the attempt.
    await waitFor(() => expect(within(screen.getByRole('list', { name: 'Attempts' })).getByText(/9\.5 g/)).toBeInTheDocument())

    await user.click(within(archives).getByRole('button', { name: 'Copy finish photo' }))
    await waitFor(() => expect(within(archives).getByRole('button', { name: 'Finish photo copied' })).toBeDisabled())
    await user.click(within(archives).getByRole('button', { name: 'Copy timelapse' }))
    await waitFor(() => expect(within(archives).getByRole('button', { name: 'Timelapse copied' })).toBeDisabled())
    const copied = state.attachments.filter((a) => a.item_id === ITEM_APPROVED)
    expect(copied.map((a) => [a.role, a.kind])).toEqual([
      ['finished', 'image'],
      ['timelapse', 'video'],
    ])
    expect(copied[0]?.caption).toBe('Attempt 1, Bambuddy archive 8001')
  })

  it('is not offered for an attempt recorded by hand', async () => {
    const attempt = printedAttempt()
    attempt.print_run_id = null
    renderPage(
      <Routes>
        <Route path="/orders/:id" element={<OrderPage />} />
      </Routes>,
      { route: `/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}` },
    )
    const attempts = await screen.findByRole('list', { name: 'Attempts' })
    expect(within(attempts).queryByRole('button', { name: 'From Bambuddy…' })).toBeNull()
  })
})
