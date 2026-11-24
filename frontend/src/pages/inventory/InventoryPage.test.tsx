import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { INVENTORY, inventoryMock, seedItem, seedLot } from '../../mocks/features/inventory'
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
    const ring = seedItem({ kind_id: 'hardware', name: 'Split ring', low_stock: 10 })
    seedLot(ring.id, 4, null)
    const { user } = render()
    const row = (await screen.findByRole('link', { name: 'Split ring' })).closest('tr')!
    expect(row).toHaveTextContent('Low')
    expect(row).toHaveTextContent('unknown')
    expect(row).not.toHaveTextContent('0.00')
    await user.click(screen.getByRole('checkbox', { name: 'Low stock only' }))
    await waitFor(async () => expect(await names()).toEqual(['Split ring']))
  })

  it('says which items have no supplier offer (R-P2-25)', async () => {
    render()
    const nozzle = (await screen.findByRole('link', { name: 'Hardened steel nozzle 0.4' })).closest('tr')!
    await waitFor(() => expect(nozzle).toHaveTextContent('no supplier'))
    const hook = screen.getByRole('link', { name: 'Silver keychain hook' }).closest('tr')!
    await waitFor(() => expect(hook).toHaveTextContent('1 offer'))
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
})
