import { screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { ITEM_APPROVED, ORDER_ACCEPTED, state } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { AttachToOrderItem } from './AttachToOrderItem'

// #4251 §9 — the print detail page's "Attach to order item": the print becomes an attempt
// of an item, naming its archive.
describe('AttachToOrderItem', () => {
  beforeEach(() => resetMockState())

  it('records the print as an attempt of the chosen item', async () => {
    const { user } = renderPage(<AttachToOrderItem open archiveId={8123} printStatus="completed" onClose={() => {}} />)

    await user.click(await screen.findByRole('combobox', { name: 'Order' }))
    await user.click(await screen.findByRole('option', { name: /ORD-2026-2/ }))
    await user.click(await screen.findByRole('combobox', { name: 'Line item' }))
    await user.click(await screen.findByRole('option', { name: /Name Keychain/ }))
    await user.clear(screen.getByLabelText('Made'))
    await user.type(screen.getByLabelText('Made'), '2')
    await user.click(screen.getByRole('button', { name: 'Attach' }))

    await waitFor(() => expect(state.fulfilments.filter((f) => f.order_item_id === ITEM_APPROVED)).toHaveLength(1))
    expect(state.fulfilments.find((f) => f.order_item_id === ITEM_APPROVED)).toMatchObject({ quantity: 2, outcome: 'succeeded', archive_ids: [8123] })
    expect(await screen.findByRole('link', { name: 'Open ORD-2026-2' })).toHaveAttribute('href', `/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}`)
  })

  // #5276: a first page made up only of orders not in work showed "No order in
  // production" while more pages remained; the server filters by status instead.
  it('offers the orders in work when the newest are all of other statuses', async () => {
    const base = state.orders.find((o) => o.id === ORDER_ACCEPTED)!
    for (let n = 0; n < 120; n++) {
      const at = new Date(Date.UTC(2026, 10, 20, 0, n)).toISOString()
      state.orders.push({ ...base, id: `0e5a3c1e-0000-4000-8000-${String(n).padStart(12, '0')}`, number: `ORD-2026-${1000 + n}`, status: 'draft', created_at: at, updated_at: at })
    }
    const { user } = renderPage(<AttachToOrderItem open archiveId={8126} printStatus="completed" onClose={() => {}} />)

    await user.click(await screen.findByRole('combobox', { name: 'Order' }))
    expect(await screen.findByRole('option', { name: /ORD-2026-2 / })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /ORD-2026-1000/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'More orders' })).not.toBeInTheDocument()
  })

  // #5273: the newest page alone once hid every order past it.
  it('finds an order past the first page, by search or by loading more', async () => {
    const base = state.orders.find((o) => o.id === ORDER_ACCEPTED)!
    for (let n = 0; n < 120; n++) {
      const at = new Date(Date.UTC(2026, 10, 20, 0, n)).toISOString()
      state.orders.push({ ...base, id: `0e5a3c1e-0000-4000-8000-${String(n).padStart(12, '0')}`, number: `ORD-2026-${1000 + n}`, status: 'in_production', created_at: at, updated_at: at })
    }
    const { user } = renderPage(<AttachToOrderItem open archiveId={8125} printStatus="completed" onClose={() => {}} />)

    await user.click(await screen.findByRole('button', { name: 'More orders' }))
    await user.click(await screen.findByRole('combobox', { name: 'Order' }))
    expect(await screen.findByRole('option', { name: /ORD-2026-2 / })).toBeInTheDocument()
    await user.keyboard('{Escape}')

    await user.type(screen.getByLabelText('Search orders'), 'ORD-2026-2')
    await waitFor(() => expect(screen.queryByRole('button', { name: 'More orders' })).not.toBeInTheDocument())
    await user.click(screen.getByRole('combobox', { name: 'Order' }))
    expect(await screen.findByRole('option', { name: /ORD-2026-2 / })).toBeInTheDocument()
  })

  it('records a failed print as a failed attempt', async () => {
    const { user } = renderPage(<AttachToOrderItem open archiveId={8124} printStatus="failed" onClose={() => {}} />)
    await user.click(await screen.findByRole('combobox', { name: 'Order' }))
    await user.click(await screen.findByRole('option', { name: /ORD-2026-2/ }))
    await user.click(await screen.findByRole('combobox', { name: 'Line item' }))
    await user.click(await screen.findByRole('option', { name: /Name Keychain/ }))
    expect(screen.getByLabelText('Made')).toHaveValue('0')
    await user.click(screen.getByRole('button', { name: 'Attach' }))
    await waitFor(() => expect(state.fulfilments.find((f) => f.order_item_id === ITEM_APPROVED)).toMatchObject({ outcome: 'failed', quantity: 0, archive_ids: [8124] }))
  })
})
