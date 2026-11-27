import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { printRunPoll } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { INVENTORY, inventoryMock, settleSlowWrites, slowNextWrite } from '../../mocks/features/inventory'
import { choose } from '../../test/select'
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

  it('an edited form after a 202 sends a new key', async () => {
    const { user } = render(INVENTORY.HOOK)
    await screen.findByRole('table', { name: 'Lots' })
    slowNextWrite(2)
    const form = await receive(user)
    await user.click(within(form).getByRole('button', { name: 'Receive lot' }))
    await within(form).findByRole('status')
    const qty = within(form).getByLabelText('Quantity')
    await user.clear(qty)
    await user.type(qty, '50')
    await user.click(within(form).getByRole('button', { name: 'Receive lot' }))
    await waitFor(() => expect(inventoryMock.keys.filter((k) => k.route === 'receiveLot')).toHaveLength(2))
    const [first, second] = inventoryMock.keys.filter((k) => k.route === 'receiveLot')
    expect(second?.key).not.toBe(first?.key)

    settleSlowWrites()
    await waitFor(() => expect(lotRows()).toHaveLength(4))
  })

  it('says the change was saved when its operation no longer holds the answer', async () => {
    const { user } = render(INVENTORY.HOOK)
    await screen.findByRole('table', { name: 'Lots' })
    slowNextWrite()
    const form = await receive(user)
    await user.click(within(form).getByRole('button', { name: 'Receive lot' }))
    await within(form).findByRole('status')

    settleSlowWrites({ gone: true })
    expect(await screen.findByRole('alert')).toHaveTextContent(/saved, but ScadBuddy no longer has its answer/)
  })

  it("shows a unit's slow status change in its own row only", async () => {
    const { user } = render(INVENTORY.NOZZLE)
    const units = await screen.findByRole('table', { name: 'Units' })
    slowNextWrite()
    await choose(user, within(units).getByRole('combobox', { name: 'Status of HS-0001' }), 'In service')
    const [, first, second] = within(screen.getByRole('table', { name: 'Units' })).getAllByRole('row')
    expect(await within(first!).findByRole('status')).toHaveTextContent('Still working')
    expect(within(second!).queryByRole('status')).toBeNull()

    settleSlowWrites()
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
  })
})
