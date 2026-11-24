import { screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { INVENTORY, inventoryMock } from '../../mocks/features/inventory'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { UnitPage } from './UnitPage'

function render(id: string) {
  return renderPage(
    <Routes>
      <Route path="/inventory/units/:id" element={<UnitPage />} />
    </Routes>,
    { route: `/inventory/units/${id}` },
  )
}

describe('UnitPage (#7067)', () => {
  beforeEach(() => resetMockState())

  it('names the unit by its item and serial, with its cost and a link back to the item', async () => {
    render(INVENTORY.NOZZLE_UNIT)
    expect(await screen.findByRole('heading', { level: 1, name: 'Hardened steel nozzle 0.4 HS-0001' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Hardened steel nozzle 0.4' })).toHaveAttribute('href', `/inventory/items/${INVENTORY.NOZZLE}`)
    expect(screen.getByText('$19.99')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Files, links and properties' })).toBeInTheDocument()
  })

  it('changes its status with a note', async () => {
    const { user } = render(INVENTORY.NOZZLE_UNIT)
    await screen.findByRole('heading', { level: 1 })
    await choose(user, screen.getByRole('combobox', { name: 'New status' }), 'Retired')
    await user.type(screen.getByLabelText('Note'), 'worn out')
    await user.click(screen.getByRole('button', { name: 'Change status' }))
    await waitFor(() => expect(screen.getByTestId('unit-status')).toHaveTextContent('Retired'))
    expect(inventoryMock.bodies.filter((b) => b.route === 'unitStatus').map((b) => b.body)).toEqual([{ status: 'retired', note: 'worn out' }])
  })

  it('says when there is no such unit', async () => {
    render('ab710000-0000-4000-8000-0000000000ff')
    expect(await screen.findByRole('alert')).toHaveTextContent('No such unit')
  })
})
