import { screen, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { KEYCHAIN, TAG } from '../../mocks/features/storefrontPublic'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { ListingsPage } from './ListingsPage'

describe('ListingsPage', () => {
  it('lists each published listing and offers a design request', async () => {
    renderPage(<ListingsPage />)
    const card = (await screen.findByRole('heading', { name: 'Name keychain' })).closest('article')!
    expect(within(card).getByRole('link', { name: 'Customize Name keychain' })).toHaveAttribute('href', '/l/l1')
    expect(screen.getByRole('link', { name: 'Ask for a design' })).toHaveAttribute('href', '/request')
  })

  it('marks a listing with no colour left as unavailable', async () => {
    renderPage(<ListingsPage />)
    const card = (await screen.findByRole('heading', { name: 'Luggage tag' })).closest('article')!
    expect(within(card).getByText('Not available right now')).toBeInTheDocument()
  })

  it('hides the design request when the shop takes none', async () => {
    server.use(http.get('/api/store/v1/listings', () => HttpResponse.json({ listings: [KEYCHAIN, TAG], requests_enabled: false })))
    renderPage(<ListingsPage />)
    await screen.findByRole('heading', { name: 'Name keychain' })
    expect(screen.queryByRole('link', { name: 'Ask for a design' })).toBeNull()
  })

  it('renders the shop text as text', async () => {
    const hostile = { ...KEYCHAIN, title: '<b>bold</b>', description: '<script>x()</script>' }
    server.use(http.get('/api/store/v1/listings', () => HttpResponse.json({ listings: [hostile], requests_enabled: false })))
    renderPage(<ListingsPage />)
    expect(await screen.findByRole('heading', { name: '<b>bold</b>' })).toBeInTheDocument()
    expect(screen.getByText('<script>x()</script>')).toBeInTheDocument()
    expect(document.querySelector('b')).toBeNull()
  })
})
