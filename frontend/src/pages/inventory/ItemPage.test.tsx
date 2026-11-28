import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { failNextRecost, INVENTORY, inventoryMock, seedItem, seedLot, takeStock } from '../../mocks/features/inventory'
import { seedEntityProperties } from '../../mocks/features/entities'
import { inventoryApi } from './api'
import { stockFollow } from '../../api/client'
import { server } from '../../mocks/server'
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
const broken = (method: 'get' | 'post', path: string) => http[method](`/api/v1${path}`, () => HttpResponse.json({ detail: 'down' }, { status: 500 }))

describe('ItemPage (#7067)', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it("names the item with its kind path, and links each offer's page in a new tab", async () => {
    render(INVENTORY.HOOK)
    expect(await screen.findByRole('heading', { level: 1, name: 'Silver keychain hook' })).toBeInTheDocument()
    const path = screen.getByRole('navigation', { name: 'Kind' })
    expect(within(path).getByRole('link', { name: 'Consumable' })).toHaveAttribute('href', '/inventory?kind=consumable')
    expect(within(path).getByRole('link', { name: 'Hardware' })).toHaveAttribute('href', '/inventory?kind=hardware')
    const offers = await screen.findByRole('table', { name: 'Offers' })
    const link = within(offers).getByRole('link', { name: /amazon\.com/ })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(within(offers).getByRole('link', { name: 'Amazon' })).toHaveAttribute('href', expect.stringMatching(/^\/filament\/suppliers\//))
    expect(offers).toHaveTextContent('$5.99')
    expect(document.getElementById(`offer-${INVENTORY.HOOK_OFFER}`)).not.toBeNull()
  })

  it("shows the item kind's code by its name (the owner, 2026-11-27)", async () => {
    render(INVENTORY.HOOK)
    await screen.findByRole('heading', { level: 1, name: 'Silver keychain hook' })
    const hardware = inventoryMock.kinds.find((k) => k.id === 'hardware')!
    const path = screen.getByRole('navigation', { name: 'Kind' })
    expect(await within(path).findByText(`K-${hardware.number}`)).toBeInTheDocument()
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
    expect(await screen.findByText(/no supplier offer/i)).toBeInTheDocument()
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

  it('scrolls to the lot an address names once the lots have rendered', async () => {
    const scrolled: string[] = []
    const own = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = vi.fn(function (this: Element) {
      scrolled.push(this.id)
    })
    try {
      render(INVENTORY.HOOK, `#lot-${INVENTORY.HOOK_LOT}`)
      await screen.findByTestId(`lot-row-${INVENTORY.HOOK_LOT}`)
      await waitFor(() => expect(scrolled).toContain(`lot-${INVENTORY.HOOK_LOT}`))
    } finally {
      Element.prototype.scrollIntoView = own
    }
  })

  it('says when the offers cannot be read, and still shows the stock', async () => {
    server.use(broken('get', '/inventory/items/:id/offers'))
    render(INVENTORY.HOOK)
    expect(await screen.findByText(/The offers could not be loaded/)).toHaveAttribute('role', 'alert')
    expect(await screen.findByRole('table', { name: 'Lots' })).toBeInTheDocument()
    expect(screen.queryByText(/no supplier offer/i)).toBeNull()
  })

  it('says when the stock moves cannot be read', async () => {
    server.use(broken('get', '/inventory/moves'))
    render(INVENTORY.HOOK)
    expect(await screen.findByText(/The stock moves could not be loaded/)).toHaveAttribute('role', 'alert')
  })

  it('says when the places cannot be read', async () => {
    server.use(broken('get', '/filament/locations'))
    render(INVENTORY.HOOK)
    expect(await screen.findByText(/The places could not be loaded/)).toHaveAttribute('role', 'alert')
  })

  it('keeps every cost input off when the shop currency cannot be read (R-P2-41)', async () => {
    server.use(broken('get', '/orders/price-book'))
    const { user } = render(INVENTORY.HOOK)
    expect(await screen.findByText(/The shop currency could not be read/)).toHaveAttribute('role', 'alert')
    const row = await screen.findByTestId(`lot-row-${INVENTORY.HOOK_LOT}`)
    expect(within(row).getByRole('button', { name: 'Edit cost' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Receive' }))
    const form = screen.getByRole('form', { name: 'Receive a lot' })
    expect(within(form).getByLabelText(/Total cost/)).toBeDisabled()
    expect(within(form).getByLabelText(/Total cost/)).toHaveValue('')
  })

  it('shows a lot bought in another currency at that currency\'s own digits', async () => {
    const ring = seedItem({ kind_id: 'hardware', name: 'Split ring' })
    const lot = seedLot(ring.id, 10, null, { cost_currency: 'JPY', foreign_unit_cost: '1500' })
    render(ring.id)
    const row = await screen.findByTestId(`lot-row-${lot.id}`)
    expect(row).toHaveTextContent('bought at ¥1,500 a unit')
    expect(row).not.toHaveTextContent('minor units')
  })

  it('offers "Re-cost past uses" on every lot with uses, and sends one request at a time', async () => {
    const ring = seedItem({ kind_id: 'hardware', name: 'Split ring' })
    const lot = seedLot(ring.id, 10, 25)
    takeStock(ring.id, 4, { order_id: ORDER_ACCEPTED, order_item_id: ITEM_APPROVED })
    let calls = 0
    server.use(
      http.post('/api/v1/inventory/lots/:id/recost', () => {
        calls += 1
        return new Promise<never>(() => undefined)
      }),
    )
    const { user } = render(ring.id)
    const row = await screen.findByTestId(`lot-row-${lot.id}`)
    const button = within(row).getByRole('button', { name: 'Re-cost past uses' })
    await user.click(button)
    expect(await within(row).findByRole('button', { name: 'Re-costing…' })).toBeDisabled()
    await user.click(within(row).getByRole('button', { name: 'Re-costing…' }))
    expect(calls).toBe(1)
  })

  // #6949 K1 Task 16, fix round 1: a re-cost still running answers 202; the page follows it
  // with the same key until the answer comes, then says what it changed.
  it('follows a re-cost that answers 202 to its end', async () => {
    stockFollow.intervalMs = 1
    const ring = seedItem({ kind_id: 'hardware', name: 'Split ring' })
    const lot = seedLot(ring.id, 10, 25)
    takeStock(ring.id, 4, { order_id: ORDER_ACCEPTED, order_item_id: ITEM_APPROVED })
    const keys: (string | null)[] = []
    let release = () => {}
    const held = new Promise<void>((resolve) => (release = resolve))
    server.use(
      http.post('/api/v1/inventory/lots/:id/recost', async ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        if (keys.length === 1) return HttpResponse.json({ workflow_id: `lot:${lot.id}:recost:k`, kind: 'RecostLot', started_at: '2026-11-27T00:00:00Z', repeated: false }, { status: 202 })
        await held
        return undefined
      }),
    )
    const { user } = render(ring.id)
    const row = await screen.findByTestId(`lot-row-${lot.id}`)
    await user.click(within(row).getByRole('button', { name: 'Re-cost past uses' }))

    const working = await screen.findByText(/Still working/)
    expect(working.closest('[data-operation]')).toHaveAttribute('data-operation', `lot:${lot.id}:recost:k`)
    release()

    await waitFor(() => expect(screen.queryByText(/Still working/)).toBeNull())
    expect(keys.length).toBe(2)
    expect(keys[1]).toBe(keys[0])
  })

  it('offers no re-cost on a lot nothing has taken from', async () => {
    render(INVENTORY.HOOK)
    const row = await screen.findByTestId(`lot-row-${INVENTORY.HOOK_LOT}`)
    expect(within(row).queryByRole('button', { name: 'Re-cost past uses' })).toBeNull()
  })

  it("shows the orders problem when new order moves keep reaching the lot (recost_busy)", async () => {
    const ring = seedItem({ kind_id: 'hardware', name: 'Split ring' })
    const lot = seedLot(ring.id, 10, 25)
    takeStock(ring.id, 4, { order_id: ORDER_ACCEPTED, order_item_id: ITEM_APPROVED })
    failNextRecost()
    const { user } = render(ring.id)
    const row = await screen.findByTestId(`lot-row-${lot.id}`)
    await user.click(within(row).getByRole('button', { name: 'Re-cost past uses' }))
    expect(await screen.findByText(/kept taking new order moves/)).toHaveAttribute('role', 'alert')
  })

  it('checks one serial a line for each unit, and shows a serial the item already has', async () => {
    const { user } = render(INVENTORY.NOZZLE)
    await screen.findByRole('table', { name: 'Units' })
    await user.click(screen.getByRole('button', { name: 'Receive' }))
    const form = screen.getByRole('form', { name: 'Receive units' })
    const count = within(form).getByLabelText('Count')
    await user.clear(count)
    await user.type(count, '2')
    const serials = within(form).getByLabelText('Serials, one a line')
    await user.type(serials, 'HS-0009')
    await user.click(within(form).getByRole('button', { name: 'Receive units' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('Give one serial a line for each of the 2 units, or none.')
    expect(bodyOf('receiveUnits')).toEqual([])
    await user.type(serials, '{Enter}HS-0001')
    await user.click(within(form).getByRole('button', { name: 'Receive units' }))
    await waitFor(() => expect(within(form).getByRole('alert')).toHaveTextContent('Another unit of this item has that serial.'))
    expect(bodyOf('receiveUnits')).toEqual([{ count: 2, serials: ['HS-0009', 'HS-0001'] }])
  })

  it('adds an offer, and links the suppliers page from the form', async () => {
    const { user } = render(INVENTORY.HOOK)
    await screen.findByRole('table', { name: 'Offers' })
    await user.click(screen.getByRole('button', { name: 'Add offer' }))
    const form = screen.getByRole('form', { name: 'Add offer' })
    expect(within(form).getByRole('link', { name: 'Filament → Suppliers' })).toHaveAttribute('href', '/filament/suppliers')
    await choose(user, within(form).getByRole('combobox', { name: 'Supplier' }), 'Amazon')
    await user.type(within(form).getByLabelText('SKU'), 'B0HOOK500')
    const size = within(form).getByLabelText('Pack size')
    await user.clear(size)
    await user.type(size, '500.0')
    await user.click(within(form).getByRole('button', { name: 'Add offer' }))
    await waitFor(() => expect(within(screen.getByRole('table', { name: 'Offers' })).getAllByRole('row')).toHaveLength(3))
    expect(screen.getByRole('table', { name: 'Offers' })).toHaveTextContent('500 each')
    expect(bodyOf('createOffer')).toEqual([{ supplier_id: expect.any(String), item_id: INVENTORY.HOOK, sku: 'B0HOOK500', url: null, pack_size: '500.0', pack_uom: 'each' }])
  })

  it('says when the suppliers cannot be read for a new offer', async () => {
    server.use(broken('get', '/filament/suppliers'))
    const { user } = render(INVENTORY.HOOK)
    await screen.findByRole('table', { name: 'Offers' })
    await user.click(screen.getByRole('button', { name: 'Add offer' }))
    const form = screen.getByRole('form', { name: 'Add offer' })
    expect(await within(form).findByText(/The suppliers could not be loaded/)).toHaveAttribute('role', 'alert')
  })

  it('archives an offer', async () => {
    const { user } = render(INVENTORY.HOOK)
    const offers = await screen.findByRole('table', { name: 'Offers' })
    await user.click(within(offers).getByRole('button', { name: 'Archive offer B0HOOK100' }))
    expect(await screen.findByText(/No offers yet/)).toBeInTheDocument()
  })

  it('archives the item and restores it', async () => {
    const { user } = render(INVENTORY.HOOK)
    await screen.findByRole('heading', { level: 1, name: 'Silver keychain hook' })
    await user.click(screen.getByRole('button', { name: 'Archive' }))
    expect(await screen.findByText('Archived')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(screen.queryByText('Archived')).toBeNull())
    expect(screen.getByRole('button', { name: 'Archive' })).toBeInTheDocument()
  })

  describe('required properties (R-K1-33)', () => {
    async function strictItem(values: Record<string, string> = {}) {
      await inventoryApi.createKind({ id: 'strict', name: 'Strict', parent_id: 'hardware', attribute_schema: { type: 'object', properties: { material: { type: 'string' }, finish: { type: 'string' } }, required: ['material'] } })
      const made = seedItem({ kind_id: 'strict', name: 'Bolt' })
      if (Object.keys(values).length) seedEntityProperties(`item:${made.id}`, values)
      return made
    }

    it('marks an item that lacks a required property', async () => {
      const made = await strictItem()
      render(made.id)
      await screen.findByRole('heading', { level: 1, name: 'Bolt' })
      expect(await screen.findByText('Missing required: material')).toBeInTheDocument()
    })

    it("says when the kind's properties could not be loaded", async () => {
      const made = await strictItem()
      server.use(http.get('/api/v1/inventory/kinds/strict/schema', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
      const { user } = render(made.id)
      await screen.findByRole('heading', { level: 1, name: 'Bolt' })
      await user.click(screen.getByRole('button', { name: 'Edit details' }))
      const form = screen.getByRole('form', { name: 'Item details' })
      expect(await within(form).findByText(/The kind's properties could not be loaded/)).toBeInTheDocument()
    })

    it("edits the kind's keys with the item and sends both in one request", async () => {
      const made = await strictItem({ material: 'steel', finish: 'zinc' })
      const { user } = render(made.id)
      await screen.findByRole('heading', { level: 1, name: 'Bolt' })
      await user.click(screen.getByRole('button', { name: 'Edit details' }))
      const form = screen.getByRole('form', { name: 'Item details' })
      const material = await within(form).findByRole('textbox', { name: 'material' })
      await waitFor(() => expect(material).toHaveValue('steel'))
      // The panel lists only the keys the form does not hold.
      expect(within(screen.getByRole('region', { name: 'Properties' })).queryByText('zinc')).toBeNull()

      await user.clear(material)
      await user.click(within(form).getByRole('button', { name: 'Save details' }))
      expect(await within(form).findByRole('alert')).toHaveTextContent('Fill the required properties: material')
      expect(material).toHaveAttribute('aria-invalid', 'true')

      await user.type(material, 'brass')
      await user.click(within(form).getByRole('button', { name: 'Save details' }))
      await waitFor(() => expect(screen.queryByRole('form', { name: 'Item details' })).toBeNull())
      expect(bodyOf('patchItem').at(-1)).toMatchObject({ name: 'Bolt', properties: { material: 'brass' } })
      expect(bodyOf('patchItem').filter((b) => 'properties' in (b as object))).toHaveLength(2)
    })
  })
})
