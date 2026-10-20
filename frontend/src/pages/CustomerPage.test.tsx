import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { resetMockState } from '../mocks/handlers'
import { CUSTOMER_ADA } from '../mocks/features/orders/state'
import { renderPage } from '../test/utils'
import { CustomerPage } from './CustomerPage'

describe('CustomerPage (#4249)', () => {
  beforeEach(() => resetMockState())

  it('shows the record with its orders, and saves an edit keeping the rows by id', async () => {
    const { user } = renderPage(
      <Routes>
        <Route path="/customers/:id" element={<CustomerPage />} />
      </Routes>,
      { route: `/customers/${CUSTOMER_ADA}` },
    )
    expect(await screen.findByRole('heading', { name: 'Ada Lovelace' })).toBeInTheDocument()
    const orders = await screen.findByRole('table', { name: 'Customer orders' })
    expect(within(orders).getAllByRole('link').map((l) => l.textContent)).toEqual(['ORD-2026-3', 'ORD-2026-2'])
    const before = await api.customer(CUSTOMER_ADA)
    const name = screen.getByLabelText('Name')
    await user.clear(name)
    await user.type(name, 'Ada King')
    await user.click(screen.getByRole('button', { name: 'Save customer' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Ada King' })).toBeInTheDocument())
    const after = await api.customer(CUSTOMER_ADA)
    expect(after.addresses[0]?.id).toBe(before.addresses[0]?.id)
    expect(after.contacts.map((c) => c.id)).toEqual(before.contacts.map((c) => c.id))
  })

  it('archives the customer', async () => {
    const { user } = renderPage(
      <Routes>
        <Route path="/customers/:id" element={<CustomerPage />} />
      </Routes>,
      { route: `/customers/${CUSTOMER_ADA}` },
    )
    await screen.findByRole('heading', { name: 'Ada Lovelace' })
    await user.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(screen.getByText(/^Archived /)).toBeInTheDocument())
  })
})
