import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
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
    expect(within(shelf).getByText('Above the limit')).toBeInTheDocument()
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

describe('the Locations tab when the sensors fail', () => {
  it('still shows the places, and an error with Retry where the sensors would be', async () => {
    let fail = true
    server.use(http.get('/api/v1/filament/sensors', () => (fail ? HttpResponse.json({ detail: 'down' }, { status: 500 }) : undefined)))
    const { user } = renderPage(<LocationsTab />)
    await place(L.WORKSHOP)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The sensors could not be loaded')
    fail = false
    await user.click(within(alert.parentElement!).getByRole('button', { name: 'Retry' }))
    const shelf = await place(L.SHELF_A)
    await waitFor(() => expect(within(shelf).getByText('Shelf A humidity')).toBeInTheDocument())
    expect(screen.queryByText(/The sensors could not be loaded/)).toBeNull()
  })
})
