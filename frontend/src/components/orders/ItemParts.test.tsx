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
    // #5549 review: every part is in the list now, so there is none left to add.
    expect(screen.getByRole('button', { name: 'Add part' })).toBeDisabled()
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

  it("a fulfilment takes the parts as the item's parts cost, and an unfulfil gives them back", async () => {
    const hook = seedPart({ name: 'Silver hook', pack_size: 100, pack_price_minor: 599, on_hand: 10 })
    await api.setItemParts(ORDER_ACCEPTED, ITEM_APPROVED, [{ part_id: hook.id, per_unit: 1 }])

    const attempt = await api.recordFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 2, outcome: 'succeeded' })
    await api.acceptFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, attempt.id)

    expect((await api.parts())[0]?.on_hand).toBe(8)
    // #7067: job cost reads them as the item's `parts_minor`; no cost entry is written.
    const costs = await api.orderCosts(ORDER_ACCEPTED)
    const item = costs.items.find((i) => i.item_id === ITEM_APPROVED)!
    expect(item.parts_minor).toBe(12)
    expect((item.cost_entries ?? []).some((e) => e.source?.startsWith('part:'))).toBe(false)

    await api.unfulfilOrderItem(ORDER_ACCEPTED, ITEM_APPROVED)
    expect((await api.parts())[0]?.on_hand).toBe(10)
    const after = await api.orderCosts(ORDER_ACCEPTED)
    expect(after.items.find((i) => i.item_id === ITEM_APPROVED)!.parts_minor).toBe(0)
  })
})
