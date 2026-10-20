import { screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../mocks/handlers'
import { renderPage } from '../test/utils'
import { CustomersPage } from './CustomersPage'

function Location() {
  const location = useLocation()
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>
}

describe('CustomersPage (#4249)', () => {
  beforeEach(() => resetMockState())

  it('lists customers by name and searches by any contact point', async () => {
    const { user } = renderPage(
      <>
        <CustomersPage />
        <Location />
      </>,
      { route: '/customers' },
    )
    const table = await screen.findByRole('table', { name: 'Customers' })
    expect(within(table).getAllByRole('link').map((l) => l.textContent)).toEqual(['Ada Lovelace', 'Bob Builder'])
    await user.type(screen.getByRole('searchbox', { name: 'Search customers' }), '@bob')
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('q=%40bob'))
    await waitFor(() => expect(within(screen.getByRole('table', { name: 'Customers' })).getAllByRole('link')).toHaveLength(1))
  })

  it('creates a customer from the drawer and moves to it', async () => {
    const { user } = renderPage(
      <>
        <CustomersPage />
        <Location />
      </>,
      { route: '/customers' },
    )
    await screen.findByRole('table', { name: 'Customers' })
    await user.click(screen.getByRole('button', { name: 'New customer' }))
    const drawer = screen.getByRole('dialog', { name: 'New customer' })
    await user.type(within(drawer).getByLabelText('Name'), 'Grace Hopper')
    await user.click(within(drawer).getByRole('button', { name: 'Add contact' }))
    await user.type(within(drawer).getByLabelText('Contact 1 value'), 'grace@example.test')
    await user.click(within(drawer).getByRole('button', { name: 'Create customer' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/customers\/c-/))
  })
})
