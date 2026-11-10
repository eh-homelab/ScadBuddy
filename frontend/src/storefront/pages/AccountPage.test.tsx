import { screen, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { ACCOUNT_ORDERS } from '../../mocks/features/storefrontAccount'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AccountPage } from './AccountPage'

const SIGNED_IN = [
  http.get('/api/store/v1/account', () => HttpResponse.json({ email: 'ada@example.com' })),
  http.get('/api/store/v1/account/orders', () => HttpResponse.json({ orders: ACCOUNT_ORDERS })),
]

describe('AccountPage', () => {
  it('asks a visitor who is not signed in to sign in', async () => {
    renderPage(<AccountPage />)
    expect(await screen.findByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/account/sign-in')
  })

  it("lists the account's orders, each linking to its order page", async () => {
    server.use(...SIGNED_IN)
    renderPage(<AccountPage />)
    expect(await screen.findByText('ada@example.com')).toBeInTheDocument()
    const list = await screen.findByRole('list', { name: 'Your orders' })
    const link = within(list).getByRole('link', { name: /ORD-2026-14/ })
    expect(link).toHaveAttribute('href', '/o?order=o-2')
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
  })

  it('says so when the account has no orders', async () => {
    server.use(http.get('/api/store/v1/account/orders', () => HttpResponse.json({ orders: [] })), ...SIGNED_IN)
    renderPage(<AccountPage />)
    expect(await screen.findByText('You have no orders yet.')).toBeInTheDocument()
  })

  it('signs out', async () => {
    let posted = false
    server.use(
      ...SIGNED_IN,
      http.post('/api/store/v1/account/sign-out', () => {
        posted = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { user } = renderPage(<AccountPage />)
    await user.click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('status')).toHaveTextContent('You are signed out.')
    expect(posted).toBe(true)
    expect(screen.queryByText('ada@example.com')).toBeNull()
  })

  it('says so when the shop could not end the session yet', async () => {
    const detail = 'You are signed out on this device. The shop could not end the session yet, and it keeps trying.'
    server.use(
      ...SIGNED_IN,
      http.post('/api/store/v1/account/sign-out', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/sign_out_incomplete', title: 'Service Unavailable', status: 503, detail },
          { status: 503, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderPage(<AccountPage />)
    await user.click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('status')).toHaveTextContent(detail)
    expect(screen.queryByText('ada@example.com')).toBeNull()
  })

  it('keeps the account shown when a sign-out failed outright', async () => {
    server.use(
      ...SIGNED_IN,
      http.post('/api/store/v1/account/sign-out', () => HttpResponse.error()),
    )
    const { user } = renderPage(<AccountPage />)
    await user.click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The shop is out of reach.')
    expect(screen.getByText('ada@example.com')).toBeInTheDocument()
  })
})
