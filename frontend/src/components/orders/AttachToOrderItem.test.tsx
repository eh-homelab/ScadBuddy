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
