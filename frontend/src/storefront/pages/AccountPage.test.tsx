import { readFileSync } from 'node:fs'
import { screen, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { ACCOUNT_ORDERS } from '../../mocks/features/storefrontAccount'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AccountPage } from './AccountPage'

/**
 * The edge's two 503 `sign_out_incomplete` bodies, as `backend/tests/storefront/
 * test_accounts_edge.py` checks the real edge answers them (#5283, review of #5955).
 */
const INCOMPLETE = JSON.parse(
  readFileSync(`${import.meta.dirname}/../../../../test-fixtures/storefront/sign-out-incomplete.json`, 'utf8'),
) as Record<'queued' | 'not_queued', { detail: string }>

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

  it.each(['queued', 'not_queued'] as const)(
    "shows the edge's text when the shop could not end the session (%s)",
    async (which) => {
      const body = INCOMPLETE[which]
      server.use(
        ...SIGNED_IN,
        http.post('/api/store/v1/account/sign-out', () =>
          HttpResponse.json(body, { status: 503, headers: { 'Content-Type': 'application/problem+json' } }),
        ),
      )
      const { user } = renderPage(<AccountPage />)
      await user.click(await screen.findByRole('button', { name: 'Sign out' }))
      expect(await screen.findByRole('status')).toHaveTextContent(body.detail)
      expect(screen.queryByText('ada@example.com')).toBeNull()
    },
  )

  it('names each status in words a customer reads, and any other as in progress', async () => {
    const statuses = ['draft', 'quoted', 'accepted', 'in_production', 'ready', 'shipped', 'closed', 'cancelled', 'on_hold']
    const orders = statuses.map((status, n) => ({ ...ACCOUNT_ORDERS[0], id: `o-${n}`, number: `ORD-${n}`, status }))
    server.use(http.get('/api/store/v1/account/orders', () => HttpResponse.json({ orders })), ...SIGNED_IN)
    renderPage(<AccountPage />)
    const items = within(await screen.findByRole('list', { name: 'Your orders' })).getAllByRole('listitem')
    const shown = items.map((item) => item.querySelector('.sf-hint')?.textContent?.split(' · ').at(-1))
    expect(shown).toEqual([
      'Received',
      'Quote ready',
      'Accepted',
      'Being made',
      'Ready',
      'Shipped',
      'Complete',
      'Cancelled',
      'In progress',
    ])
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
