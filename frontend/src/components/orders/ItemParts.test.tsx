import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { INVENTORY, seedItem, seedLot } from '../../mocks/features/inventory'
import { server } from '../../mocks/server'
import { inventoryApi } from '../../pages/inventory/api'
import { ITEM_APPROVED, ORDER_ACCEPTED } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { ItemPartsEditor, PartsShort } from './ItemParts'

/** A hook item with `onHand` in one lot at 5.99 cents each (a pack of 100 at 5.99). */
function seedHook(onHand: number) {
  const hook = seedItem({ kind_id: 'hardware', name: 'Silver hook' })
  if (onHand > 0) seedLot(hook.id, onHand, 5.99)
  return hook
}

/** The bodies each PUT of an item's parts sent. */
function recordedPuts() {
  const bodies: unknown[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === 'PUT' && /\/items\/[^/]+\/parts$/.test(new URL(request.url).pathname)) void request.clone().json().then((body) => bodies.push(body))
  })
  return bodies
}

describe('item parts (#5543, #7067)', () => {
  beforeEach(() => resetMockState())

  it('names each part an order is short of', async () => {
    const hook = seedHook(1)
    await api.setItemParts(ORDER_ACCEPTED, ITEM_APPROVED, [{ item_id: hook.id, per_unit: 2 }])
    const parts = await api.orderParts(ORDER_ACCEPTED)

    renderPage(<PartsShort parts={parts} />)

    // Two units of the item, two hooks each, one on hand.
    expect(screen.getByTestId('parts-short')).toHaveTextContent('short: 3 Silver hook')
    expect(screen.getByRole('link', { name: 'Silver hook' })).toHaveAttribute('href', `/inventory/items/${hook.id}`)
  })

  it('sets the parts by hand', async () => {
    const user = userEvent.setup()
    const hook = seedHook(10)
    const puts = recordedPuts()
    const onChanged = vi.fn()
    renderPage(<ItemPartsEditor orderId={ORDER_ACCEPTED} item={{ id: ITEM_APPROVED, quantity: 2, status: 'approved' }} rows={[]} editable onChanged={onChanged} />)

    expect(screen.getByText('No parts.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Edit parts' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add part' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Add part' }))
    // #7067: the picker lists the inventory's lot items only (the seeded nozzle is counted in units).
    await user.click(screen.getByRole('button', { name: 'Add part' }))
    // #5549 review: every part is in the list now, so there is none left to add.
    expect(screen.getByRole('button', { name: 'Add part' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Save parts' }))

    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(puts).toEqual([{ parts: [{ item_id: hook.id, per_unit: 1 }, { item_id: INVENTORY.HOOK, per_unit: 1 }] }])
    const parts = await api.orderParts(ORDER_ACCEPTED)
    expect(parts.items.find((i) => i.item_id === ITEM_APPROVED)?.parts).toEqual([
      { item_id: hook.id, part_id: hook.id, per_unit: 1, used: 0 },
      { item_id: INVENTORY.HOOK, part_id: INVENTORY.HOOK, per_unit: 1, used: 0 },
    ])
  })

  it('fixes a fulfilled item’s parts', async () => {
    const hook = seedHook(0)
    renderPage(
      <ItemPartsEditor orderId={ORDER_ACCEPTED} item={{ id: ITEM_APPROVED, quantity: 2, status: 'fulfilled' }} rows={[{ item_id: hook.id, part_id: hook.id, per_unit: 1, used: 2 }]} editable={false} onChanged={() => {}} />,
    )
    const row = await screen.findByRole('listitem')
    await waitFor(() => expect(row).toHaveTextContent(/^1 × Silver hook a unit \(2 taken from stock\)$/))
    // #7067: each part links to its inventory item.
    expect(screen.getByRole('link', { name: 'Silver hook' })).toHaveAttribute('href', `/inventory/items/${hook.id}`)
    expect(screen.queryByRole('button', { name: 'Edit parts' })).not.toBeInTheDocument()
    expect(screen.getByText(/unfulfil it to change them/)).toBeInTheDocument()
  })

  it("a fulfilment takes the parts as the item's parts cost, and an unfulfil gives them back", async () => {
    const hook = seedHook(10)
    await api.setItemParts(ORDER_ACCEPTED, ITEM_APPROVED, [{ item_id: hook.id, per_unit: 1 }])

    const attempt = await api.recordFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 2, outcome: 'succeeded' })
    await api.acceptFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, attempt.id)

    expect((await inventoryApi.item(hook.id)).on_hand).toBe('8')
    // #7067: job cost reads them as the item's `parts_minor`; no cost entry is written.
    const costs = await api.orderCosts(ORDER_ACCEPTED)
    const item = costs.items.find((i) => i.item_id === ITEM_APPROVED)!
    expect(item.parts_minor).toBe(12)
    expect((item.cost_entries ?? []).some((e) => e.source?.startsWith('part:'))).toBe(false)

    await api.unfulfilOrderItem(ORDER_ACCEPTED, ITEM_APPROVED)
    expect((await inventoryApi.item(hook.id)).on_hand).toBe('10')
    const after = await api.orderCosts(ORDER_ACCEPTED)
    expect(after.items.find((i) => i.item_id === ITEM_APPROVED)!.parts_minor).toBe(0)
  })

  it("names an archived part on the item, and offers only unarchived ones to pick", async () => {
    const user = userEvent.setup()
    const hook = seedHook(10)
    const old = seedHook(0)
    await inventoryApi.patchItem(old.id, { name: 'Old hook' })
    await inventoryApi.archiveItem(old.id)
    renderPage(<ItemPartsEditor orderId={ORDER_ACCEPTED} item={{ id: ITEM_APPROVED, quantity: 2, status: 'approved' }} rows={[{ item_id: old.id, part_id: old.id, per_unit: 1, used: 0 } as never]} editable onChanged={() => {}} />)
    expect(await screen.findByText(/Old hook/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Edit parts' }))
    await user.click(screen.getByRole('combobox', { name: 'Part 1' }))
    const options = (await screen.findAllByRole('option')).map((o) => o.textContent)
    expect(options).toContain('Old hook (archived)')
    expect(options).toContain(hook.name)
    expect(options.filter((name) => name === 'Old hook')).toEqual([])
  })

  it('says when the parts cannot be read', async () => {
    server.use(http.get('/api/v1/inventory/items', () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
    renderPage(<ItemPartsEditor orderId={ORDER_ACCEPTED} item={{ id: ITEM_APPROVED, quantity: 2, status: 'approved' }} rows={[]} editable onChanged={() => {}} />)
    expect(await screen.findByText(/The parts could not be loaded/)).toHaveAttribute('role', 'alert')
  })
})
