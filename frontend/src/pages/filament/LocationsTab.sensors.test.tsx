import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { L } from '../../mocks/features/filament/seed'
import { renderPage } from '../../test/utils'
import { LocationsTab } from './LocationsTab'

const place = async (id: string) => {
  const list = await screen.findByRole('list', { name: 'Locations' })
  const found = within(list).getAllByRole('listitem').find((item) => item.dataset.place === id)
  if (!found) throw new Error(`no place ${id}`)
  return found
}

describe('the Locations tab and sensors', () => {
  it('shows each place its own sensors', async () => {
    renderPage(<LocationsTab />)
    const shelf = await place(L.SHELF_A)
    await waitFor(() => expect(within(shelf).getByText('Shelf A humidity')).toBeInTheDocument())
    expect(within(shelf).getByText('Over the limit')).toBeInTheDocument()
  })

  it('shows the AMS unit its own humidity sensor, with no add button on a printer place', async () => {
    renderPage(<LocationsTab />)
    const unit = await place(L.AMS_A)
    await waitFor(() => expect(within(unit).getByText('AMS-A humidity')).toBeInTheDocument())
    expect(within(unit).queryByRole('button', { name: /Add a sensor/ })).toBeNull()
  })

  it('offers to add a sensor to a place the user keeps', async () => {
    renderPage(<LocationsTab />)
    const room = await place(L.WORKSHOP)
    expect(within(room).getByRole('button', { name: 'Add a sensor to Workshop' })).toBeInTheDocument()
  })
})
