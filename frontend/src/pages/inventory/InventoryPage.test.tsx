import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes, useLocation } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { INVENTORY, inventoryMock, seedItem, seedLot } from '../../mocks/features/inventory'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { InventoryPage } from './InventoryPage'

function Where() {
  const { pathname, search } = useLocation()
  return <p data-testid="where">{pathname + search}</p>
}

function render(route = '/inventory') {
  return renderPage(
    <Routes>
      <Route
        path="/inventory"
        element={
          <>
            <InventoryPage />
            <Where />
          </>
        }
      />
      <Route path="*" element={<Where />} />
    </Routes>,
    { route },
  )
}

const table = () => screen.findByRole('table', { name: 'Items' })
const names = async () => within(await table()).getAllByRole('link').map((link) => link.textContent)

describe('InventoryPage (#7067)', () => {
  beforeEach(() => resetMockState())

  it('lists every item with its kind, stock and value, and links each to its page', async () => {
    render()
    const hook = await screen.findByRole('link', { name: 'Silver keychain hook' })
    expect(hook).toHaveAttribute('href', `/inventory/items/${INVENTORY.HOOK}`)
    const row = hook.closest('tr')!
    expect(row).toHaveTextContent('Hardware')
    expect(row).toHaveTextContent('200')
    // 200 hooks at 5.99 cents each.
    expect(row).toHaveTextContent('$11.98')
    expect(within(screen.getByRole('link', { name: 'Laser engraving' }).closest('tr')!).getByText('not counted')).toBeInTheDocument()
  })

  it('filters by a kind and its children, and by a search, in the address', async () => {
    const { user } = render()
    await table()
    const kinds = screen.getByRole('navigation', { name: 'Kinds' })
    await user.click(within(kinds).getByRole('link', { name: 'Part' }))
    // Hardware and Nozzle are below Part; Service is not.
    await waitFor(async () => expect(await names()).toEqual(['Hardened steel nozzle 0.4', 'Silver keychain hook']))
    expect(screen.getByTestId('where')).toHaveTextContent('/inventory?kind=part')

    await user.type(screen.getByRole('searchbox', { name: 'Search items' }), 'nozzle')
    await waitFor(async () => expect(await names()).toEqual(['Hardened steel nozzle 0.4']))
    expect(screen.getByTestId('where')).toHaveTextContent('q=nozzle')
  })

  it('shows only the low items when asked, and an unknown value never as 0.00 (R-P2-35)', async () => {
    const ring = seedItem({ kind_id: 'hardware', name: 'Split ring', low_stock: '10' })
    seedLot(ring.id, 4, null)
    const { user } = render()
    const row = (await screen.findByRole('link', { name: 'Split ring' })).closest('tr')!
    expect(row).toHaveTextContent('Low')
    expect(row).toHaveTextContent('unknown')
    expect(row).not.toHaveTextContent('0.00')
    await user.click(screen.getByRole('checkbox', { name: 'Low stock only' }))
    await waitFor(async () => expect(await names()).toEqual(['Split ring']))
  })

  it('says which items have no supplier offer, from the items list alone (R-P2-25, R-P2-37)', async () => {
    const read: string[] = []
    server.events.on('request:start', ({ request }) => read.push(new URL(request.url).pathname))
    render()
    const nozzle = (await screen.findByRole('link', { name: 'Hardened steel nozzle 0.4' })).closest('tr')!
    await waitFor(() => expect(nozzle).toHaveTextContent('no supplier'))
    const hook = screen.getByRole('link', { name: 'Silver keychain hook' }).closest('tr')!
    await waitFor(() => expect(hook).toHaveTextContent('1 offer'))
    expect(read.filter((path) => path.includes('/suppliers'))).toEqual([])
  })

  it('adds an item of a kind and opens its page', async () => {
    const { user } = render('/inventory?kind=hardware')
    await table()
    await user.click(screen.getByRole('button', { name: 'New item' }))
    const form = screen.getByRole('form', { name: 'New item' })
    await user.type(within(form).getByLabelText('Name'), 'M3 bolt')
    await user.click(within(form).getByRole('button', { name: 'Add item' }))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toMatch(/^\/inventory\/items\//))
    expect(inventoryMock.bodies.find((b) => b.route === 'createItem')?.body).toMatchObject({ kind_id: 'hardware', name: 'M3 bolt' })
  })

  it('saves a model rule over the lot items (#5543, moved from the parts page)', async () => {
    const hook = seedItem({ kind_id: 'hardware', name: 'Silver hook' })
    const { user } = render('/inventory?kind=part')
    await table()
    const rules = screen.getByRole('region', { name: 'Model rules' })

    await user.type(within(rules).getByLabelText('Model slug'), 'keychain')
    await user.click(within(rules).getByRole('button', { name: 'Open rule' }))
    expect(await within(rules).findByText(/keychain has no rule/)).toBeInTheDocument()
    await waitFor(() => expect(within(rules).getByRole('button', { name: 'Add line' })).toBeEnabled())
    await user.click(within(rules).getByRole('button', { name: 'Add line' }))
    await user.click(within(rules).getByRole('button', { name: 'Save rule' }))

    expect(await within(rules).findByText('Saved.')).toBeInTheDocument()
    expect((await api.partRule('keychain')).lines).toEqual([{ per_unit: 1, item_id: hook.id, when: [] }])

    // #5549 review: a line added or removed after the save is not saved yet.
    await user.click(within(rules).getByRole('button', { name: 'Add line' }))
    expect(within(rules).queryByText('Saved.')).toBeNull()
  })

  it('never saves the opened rule under a slug typed since (#5549 review)', async () => {
    const { user } = render('/inventory?kind=hardware')
    await table()
    const rules = screen.getByRole('region', { name: 'Model rules' })
    const field = within(rules).getByLabelText('Model slug')
    await user.type(field, 'keychain')
    await user.click(within(rules).getByRole('button', { name: 'Open rule' }))
    await within(rules).findByText(/keychain has no rule/)
    await user.clear(field)
    await user.type(field, 'coaster')
    expect(within(rules).getByRole('button', { name: 'Save rule' })).toBeDisabled()
    expect(within(rules).getByText(/Open coaster's rule first/)).toBeInTheDocument()
  })

  it('shows the model rules only for the Part kind and the kinds below it (R-P2-39)', async () => {
    const { user } = render('/inventory?kind=service')
    await table()
    expect(screen.queryByRole('region', { name: 'Model rules' })).toBeNull()
    await user.click(within(screen.getByRole('navigation', { name: 'Kinds' })).getByRole('link', { name: 'Hardware' }))
    expect(await screen.findByRole('region', { name: 'Model rules' })).toBeInTheDocument()
    await user.click(within(screen.getByRole('navigation', { name: 'Kinds' })).getByRole('link', { name: 'All items' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Model rules' })).toBeNull())
  })

  it('lists archived items only when "Show archived" is on, in the address', async () => {
    seedItem({ kind_id: 'hardware', name: 'Old clip', archived_at: '2026-11-01T00:00:00Z' })
    const { user } = render()
    expect(await names()).not.toContain('Old clip')
    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/inventory?archived=true'))
    const row = within(await table()).getByRole('link', { name: 'Old clip' }).closest('tr')!
    expect(row).toHaveTextContent('Archived')
  })

  it('says when the kinds cannot be read', async () => {
    server.use(http.get('/api/v1/inventory/kinds', () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
    render()
    expect(await screen.findByText(/The kinds could not be loaded/)).toHaveAttribute('role', 'alert')
  })
})
