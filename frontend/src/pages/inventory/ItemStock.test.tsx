import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { printRunPoll } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { INVENTORY, inventoryMock, settleSlowWrites, slowNextWrite } from '../../mocks/features/inventory'
import { renderPage } from '../../test/utils'
import { ItemPage } from './ItemPage'

const interval = printRunPoll.intervalMs

function render(id: string) {
  return renderPage(
    <Routes>
      <Route path="/inventory/items/:id" element={<ItemPage />} />
    </Routes>,
    { route: `/inventory/items/${id}` },
  )
}

const lotRows = () => within(screen.getByRole('table', { name: 'Lots' })).getAllByRole('row')

async function receive(user: ReturnType<typeof render>['user']) {
  await user.click(screen.getByRole('button', { name: 'Receive' }))
  const form = screen.getByRole('form', { name: 'Receive a lot' })
  const qty = within(form).getByLabelText('Quantity')
  await user.clear(qty)
  await user.type(qty, '300')
  return form
}

describe('ItemStock: a write that runs on (#6949 K1)', () => {
  beforeEach(() => {
    resetMockState()
    printRunPoll.intervalMs = 5
  })
  afterEach(() => {
    printRunPoll.intervalMs = interval
  })

  it('a slow receive shows still working and then the lot', async () => {
    const { user } = render(INVENTORY.HOOK)
    await screen.findByRole('table', { name: 'Lots' })
    expect(lotRows()).toHaveLength(2)
    slowNextWrite()
    const form = await receive(user)
    await user.click(within(form).getByRole('button', { name: 'Receive lot' }))

    const still = await within(form).findByRole('status')
    expect(still).toHaveTextContent('Still working')
    const { id } = inventoryMock.slow[0]!
    expect(within(still).getByRole('link')).toHaveAttribute('href', `/activity?q=${encodeURIComponent(id)}`)
    expect(within(form).getByRole('button', { name: 'Receive lot' })).toBeEnabled()

    settleSlowWrites()
    await waitFor(() => expect(lotRows()).toHaveLength(3))
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('a second press reuses the key', async () => {
    const { user } = render(INVENTORY.HOOK)
    await screen.findByRole('table', { name: 'Lots' })
    slowNextWrite()
    const form = await receive(user)
    const button = within(form).getByRole('button', { name: 'Receive lot' })
    await user.click(button)
    await within(form).findByRole('status')
    await user.click(button)
    await waitFor(() => expect(inventoryMock.keys.filter((k) => k.route === 'receiveLot')).toHaveLength(2))
    const [first, second] = inventoryMock.keys.filter((k) => k.route === 'receiveLot')
    expect(first?.key).toBeTruthy()
    expect(second?.key).toBe(first?.key)

    settleSlowWrites()
    await waitFor(() => expect(lotRows()).toHaveLength(3))
  })
})
