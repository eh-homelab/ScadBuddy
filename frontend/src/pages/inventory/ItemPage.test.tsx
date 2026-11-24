import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { INVENTORY, inventoryMock, seedItem, seedLot, takeStock } from '../../mocks/features/inventory'
import { L } from '../../mocks/features/filament/seed'
import { ITEM_APPROVED, ORDER_ACCEPTED } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { ItemPage } from './ItemPage'

function render(id: string, hash = '') {
  return renderPage(
    <Routes>
      <Route path="/inventory/items/:id" element={<ItemPage />} />
    </Routes>,
    { route: `/inventory/items/${id}${hash}` },
  )
}

const bodyOf = (route: string) => inventoryMock.bodies.filter((b) => b.route === route).map((b) => b.body)

describe('ItemPage (#7067)', () => {
  beforeEach(() => resetMockState())

  it("names the item with its kind path, and links each offer's page in a new tab", async () => {
    render(INVENTORY.HOOK)
    expect(await screen.findByRole('heading', { level: 1, name: 'Silver keychain hook' })).toBeInTheDocument()
    const path = screen.getByRole('navigation', { name: 'Kind' })
    expect(within(path).getByRole('link', { name: 'Part' })).toHaveAttribute('href', '/inventory?kind=part')
    expect(within(path).getByRole('link', { name: 'Hardware' })).toHaveAttribute('href', '/inventory?kind=hardware')
    const offers = await screen.findByRole('table', { name: 'Offers' })
    const link = within(offers).getByRole('link', { name: /amazon\.com/ })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(within(offers).getByRole('link', { name: 'Amazon' })).toHaveAttribute('href', expect.stringMatching(/^\/filament\/suppliers\//))
    expect(offers).toHaveTextContent('$5.99')
    expect(document.getElementById(`offer-${INVENTORY.HOOK_OFFER}`)).not.toBeNull()
  })

  it('lists the lots and receives one at the newest offer price for the quantity (Ruling 4)', async () => {
    const { user } = render(INVENTORY.HOOK)
    const lots = await screen.findByRole('table', { name: 'Lots' })
    expect(within(lots).getAllByRole('row')).toHaveLength(2)
    expect(document.getElementById(`lot-${INVENTORY.HOOK_LOT}`)).not.toBeNull()

    await user.click(screen.getByRole('button', { name: 'Receive' }))
    const form = screen.getByRole('form', { name: 'Receive a lot' })
    const qty = within(form).getByLabelText('Quantity')
    await user.clear(qty)
    await user.type(qty, '300')
    // One pack of 100 is 5.99, so 300 cost 17.97.
    expect(within(form).getByLabelText(/Total cost/)).toHaveValue('17.97')
    await user.click(within(form).getByRole('button', { name: 'Receive lot' }))
    await waitFor(() => expect(within(screen.getByRole('table', { name: 'Lots' })).getAllByRole('row')).toHaveLength(3))
    expect(bodyOf('receiveLot')).toEqual([{ qty: '300', total_cost_minor: '1797', note: '' }])
  })

  it('receives a lot of unknown cost when no price is given, and shows it as unknown', async () => {
    const bolt = seedItem({ kind_id: 'hardware', name: 'M3 bolt' })
    const { user } = render(bolt.id)
    await screen.findByRole('heading', { level: 1, name: 'M3 bolt' })
    expect(screen.getByText(/no supplier offer/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Receive' }))
    const form = screen.getByRole('form', { name: 'Receive a lot' })
    await user.type(within(form).getByLabelText('Quantity'), '10')
    expect(within(form).getByLabelText(/Total cost/)).toHaveValue('')
    await user.click(within(form).getByRole('button', { name: 'Receive lot' }))
    const lots = await screen.findByRole('table', { name: 'Lots' })
    await waitFor(() => expect(within(lots).getAllByRole('row')).toHaveLength(2))
    expect(within(lots).getAllByRole('row')[1]).toHaveTextContent('unknown')
    expect(bodyOf('receiveLot')).toEqual([{ qty: '10', note: '' }])
  })

  it("uses some and counts the item, each with the person's reason", async () => {
    const { user } = render(INVENTORY.HOOK)
    await screen.findByRole('table', { name: 'Lots' })
    await user.click(screen.getByRole('button', { name: 'Use' }))
    const use = screen.getByRole('form', { name: 'Use some' })
    await user.type(within(use).getByLabelText('Quantity'), '5')
    await user.type(within(use).getByLabelText('Reason'), 'test print')
    await user.click(within(use).getByRole('button', { name: 'Use' }))
    await waitFor(() => expect(screen.getByTestId('item-on-hand')).toHaveTextContent('195'))

    await user.click(screen.getByRole('button', { name: 'Count' }))
    const count = screen.getByRole('form', { name: 'Count the item' })
    await user.type(within(count).getByLabelText('Count on hand'), '190')
    await user.click(within(count).getByRole('button', { name: 'Save count' }))
    expect(await within(count).findByRole('alert')).toHaveTextContent('Say why the count changed.')
    await user.type(within(count).getByLabelText('Why it changed'), 'shelf count')
    await user.click(within(count).getByRole('button', { name: 'Save count' }))
    await waitFor(() => expect(screen.getByTestId('item-on-hand')).toHaveTextContent('190'))
    expect(bodyOf('use')).toEqual([{ qty: '5', reason: 'test print' }])
    expect(bodyOf('count')).toEqual([{ on_hand: '190', note: 'shelf count' }])
  })

  it('edits a lot cost, then re-costs its past uses and links the orders it changed', async () => {
    const ring = seedItem({ kind_id: 'hardware', name: 'Split ring' })
    const lot = seedLot(ring.id, 10, null)
    takeStock(ring.id, 4, { order_id: ORDER_ACCEPTED, order_item_id: ITEM_APPROVED })
    const { user } = render(ring.id)
    const row = await screen.findByTestId(`lot-row-${lot.id}`)
    expect(row).toHaveTextContent('unknown')
    await user.click(within(row).getByRole('button', { name: 'Edit cost' }))
    await user.type(within(row).getByLabelText('Unit cost'), '0.25')
    await user.click(within(row).getByRole('button', { name: 'Save cost' }))
    await waitFor(() => expect(screen.getByTestId(`lot-row-${lot.id}`)).toHaveTextContent('$0.25'))
    expect(bodyOf('patchLot')).toEqual([{ unit_cost_minor: '25' }])

    await user.click(screen.getByRole('button', { name: 'Re-cost past uses' }))
    const result = await screen.findByRole('region', { name: 'Re-cost result' })
    expect(result).toHaveTextContent('1 move')
    const order = within(result).getByRole('link', { name: /order/i })
    expect(order).toHaveAttribute('href', `/orders/${ORDER_ACCEPTED}`)
    expect(result).toHaveTextContent('unknown')
    expect(result).toHaveTextContent('$1.00')
  })

  it("shows a unit item's units and changes one's status", async () => {
    const { user } = render(INVENTORY.NOZZLE)
    const units = await screen.findByRole('table', { name: 'Units' })
    expect(within(units).getAllByRole('row')).toHaveLength(3)
    expect(screen.queryByRole('table', { name: 'Lots' })).toBeNull()
    const first = within(units).getByRole('link', { name: 'HS-0001' })
    expect(first).toHaveAttribute('href', `/inventory/units/${INVENTORY.NOZZLE_UNIT}`)
    await choose(user, within(units).getByRole('combobox', { name: 'Status of HS-0001' }), 'In service')
    await waitFor(() => expect(screen.getByTestId('item-on-hand')).toHaveTextContent('1'))
    expect(bodyOf('unitStatus')).toEqual([{ status: 'in_service' }])
  })

  it('keeps only offers for an item of a kind that is not counted', async () => {
    render(INVENTORY.ENGRAVING)
    await screen.findByRole('heading', { level: 1, name: 'Laser engraving' })
    expect(screen.getByText(/not counted/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Receive' })).toBeNull()
    expect(screen.queryByRole('table', { name: 'Lots' })).toBeNull()
    expect(screen.queryByRole('table', { name: 'Units' })).toBeNull()
    expect(await screen.findByRole('region', { name: 'Offers' })).toBeInTheDocument()
  })

  it('adds a price to an offer', async () => {
    const { user } = render(INVENTORY.HOOK)
    const offers = await screen.findByRole('table', { name: 'Offers' })
    await user.click(within(offers).getByRole('button', { name: 'Add price for B0HOOK100' }))
    const form = screen.getByRole('form', { name: 'Add price for B0HOOK100' })
    await user.type(within(form).getByLabelText(/Pack price/), '6.49')
    await user.click(within(form).getByRole('button', { name: 'Add price' }))
    await waitFor(() => expect(screen.getByRole('table', { name: 'Offers' })).toHaveTextContent('$6.49'))
    expect(bodyOf('addPrice')).toEqual([{ price: '6.49' }])
  })

  it('says when there is no such item', async () => {
    render('ab700000-0000-4000-8000-0000000000ff')
    expect(await screen.findByRole('alert')).toHaveTextContent('No such item')
  })

  it("sets a lot's place, and receives a lot into a place (R-P2-38)", async () => {
    const { user } = render(INVENTORY.HOOK)
    const row = await screen.findByTestId(`lot-row-${INVENTORY.HOOK_LOT}`)
    expect(row).toHaveTextContent('none')
    await user.click(within(row).getByRole('button', { name: 'Edit place' }))
    await choose(user, within(row).getByRole('combobox', { name: 'Place of the lot' }), 'Workshop / Shelf A')
    await user.click(within(row).getByRole('button', { name: 'Save place' }))
    await waitFor(() => expect(screen.getByTestId(`lot-row-${INVENTORY.HOOK_LOT}`)).toHaveTextContent('Workshop / Shelf A'))
    expect(bodyOf('patchLot')).toEqual([{ location_id: L.SHELF_A }])

    await user.click(screen.getByRole('button', { name: 'Receive' }))
    const form = screen.getByRole('form', { name: 'Receive a lot' })
    await choose(user, within(form).getByRole('combobox', { name: 'Place' }), 'Workshop / Dry box 1')
    await user.click(within(form).getByRole('button', { name: 'Receive lot' }))
    await waitFor(() => expect(bodyOf('receiveLot')).toHaveLength(1))
    expect(bodyOf('receiveLot')[0]).toMatchObject({ location_id: L.DRY_BOX })
  })

  it('shows and edits the service life (R-P2-38)', async () => {
    const plate = seedItem({ kind_id: 'build_plate', name: 'Textured plate', life_months: 12 })
    const { user } = render(plate.id)
    expect(await screen.findByText('12 months')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Edit details' }))
    const form = screen.getByRole('form', { name: 'Item details' })
    const hours = within(form).getByLabelText('Life in print hours')
    await user.type(hours, '500')
    await user.click(within(form).getByRole('button', { name: 'Save details' }))
    await waitFor(() => expect(bodyOf('patchItem')).toHaveLength(1))
    expect(bodyOf('patchItem')[0]).toMatchObject({ life_hours: '500', life_months: 12 })
  })
})
