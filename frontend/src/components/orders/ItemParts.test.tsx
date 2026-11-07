import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { seedPart } from '../../mocks/features/orders/parts'
import { ITEM_APPROVED, ORDER_ACCEPTED } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { ItemPartsEditor, PartsShort } from './ItemParts'

describe('item parts (#5543)', () => {
  beforeEach(() => resetMockState())

  it('names each part an order is short of', async () => {
    const hook = seedPart({ name: 'Silver hook', pack_size: 100, pack_price_minor: 599, on_hand: 1 })
    await api.setItemParts(ORDER_ACCEPTED, ITEM_APPROVED, [{ part_id: hook.id, per_unit: 2 }])
    const parts = await api.orderParts(ORDER_ACCEPTED)

    renderPage(<PartsShort parts={parts} />)

    // Two units of the item, two hooks each, one on hand.
    expect(screen.getByTestId('parts-short')).toHaveTextContent('short: 3 Silver hook')
  })

  it('sets the parts by hand', async () => {
    const user = userEvent.setup()
    const hook = seedPart({ name: 'Silver hook', pack_size: 100, pack_price_minor: 599, on_hand: 10 })
    const onChanged = vi.fn()
    renderPage(<ItemPartsEditor orderId={ORDER_ACCEPTED} item={{ id: ITEM_APPROVED, quantity: 2, status: 'approved' }} rows={[]} editable onChanged={onChanged} />)

    expect(screen.getByText('No parts.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Edit parts' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add part' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Add part' }))
    await user.click(screen.getByRole('button', { name: 'Save parts' }))

    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    const parts = await api.orderParts(ORDER_ACCEPTED)
    expect(parts.items.find((i) => i.item_id === ITEM_APPROVED)?.parts).toEqual([{ part_id: hook.id, per_unit: 1, used: 0 }])
  })

  it('fixes a fulfilled item’s parts', async () => {
    const hook = seedPart({ name: 'Silver hook', pack_size: 100, pack_price_minor: 599 })
    renderPage(
      <ItemPartsEditor orderId={ORDER_ACCEPTED} item={{ id: ITEM_APPROVED, quantity: 2, status: 'fulfilled' }} rows={[{ part_id: hook.id, per_unit: 1, used: 2 }]} editable={false} onChanged={() => {}} />,
    )
    expect(await screen.findByText(/1 × Silver hook a unit/)).toHaveTextContent('2 taken from stock')
    expect(screen.queryByRole('button', { name: 'Edit parts' })).not.toBeInTheDocument()
    expect(screen.getByText(/unfulfil it to change them/)).toBeInTheDocument()
  })

  it('a fulfilment takes the parts as a material cost, and an unfulfil gives them back', async () => {
    const hook = seedPart({ name: 'Silver hook', pack_size: 100, pack_price_minor: 599, on_hand: 10 })
    await api.setItemParts(ORDER_ACCEPTED, ITEM_APPROVED, [{ part_id: hook.id, per_unit: 1 }])

    const attempt = await api.recordFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 2, outcome: 'succeeded' })
    await api.acceptFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, attempt.id)

    expect((await api.parts())[0]?.on_hand).toBe(8)
    const costs = await api.orderCosts(ORDER_ACCEPTED)
    const entries = costs.items.flatMap((i) => i.cost_entries ?? [])
    expect(entries.map((e) => [e.kind, e.amount_minor, e.source])).toContainEqual(['material', 12, `part:${ITEM_APPROVED}:${hook.id}`])

    await api.unfulfilOrderItem(ORDER_ACCEPTED, ITEM_APPROVED)
    expect((await api.parts())[0]?.on_hand).toBe(10)
  })
})
