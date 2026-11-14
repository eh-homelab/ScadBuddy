import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Link, useLocation } from 'react-router'
import { describe, expect, it } from 'vitest'
import { KEYCHAIN, TAG, card, listingsPage } from '../../mocks/features/storefrontPublic'
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
    server.use(http.get('/api/store/v1/listings', () => HttpResponse.json({ listings: [card(KEYCHAIN), card(TAG)], next_cursor: null, requests_enabled: false })))
    renderPage(<ListingsPage />)
    await screen.findByRole('heading', { name: 'Name keychain' })
    expect(screen.queryByRole('link', { name: 'Ask for a design' })).toBeNull()
  })

  it('renders the shop text as text', async () => {
    const hostile = { ...KEYCHAIN, title: '<b>bold</b>', description: '<script>x()</script>' }
    server.use(http.get('/api/store/v1/listings', () => HttpResponse.json({ listings: [card(hostile)], next_cursor: null, requests_enabled: false })))
    renderPage(<ListingsPage />)
    expect(await screen.findByRole('heading', { name: '<b>bold</b>' })).toBeInTheDocument()
    expect(screen.getByText('<script>x()</script>')).toBeInTheDocument()
    expect(document.querySelector('b')).toBeNull()
  })

  it('shows more designs a page at a time', async () => {
    const many = Array.from({ length: 30 }, (_, n) => ({ ...KEYCHAIN, id: `l${n + 10}`, title: `Design ${n + 1}` }))
    const asked: string[] = []
    server.use(
      http.get('/api/store/v1/listings', ({ request }) => {
        const url = new URL(request.url)
        asked.push(url.search)
        return HttpResponse.json(listingsPage(many, url))
      }),
    )
    const { user } = renderPage(<ListingsPage />)
    await screen.findByRole('heading', { name: 'Design 1' })
    expect(screen.getAllByRole('article')).toHaveLength(24)
    await user.click(screen.getByRole('button', { name: 'Show more designs' }))
    await screen.findByRole('heading', { name: 'Design 30' })
    expect(screen.getAllByRole('article')).toHaveLength(30)
    expect(screen.queryByRole('button', { name: 'Show more designs' })).toBeNull()
    expect(asked).toEqual(['', '?cursor=24'])
  })

  it('searches the designs by title, once the visitor stops typing', async () => {
    const asked: string[] = []
    server.events.on('request:start', ({ request }) => {
      const url = new URL(request.url)
      if (url.pathname === '/api/store/v1/listings') asked.push(url.searchParams.get('q') ?? '')
    })
    const { user } = renderPage(<ListingsPage debounceMs={50} />)
    await screen.findByRole('heading', { name: 'Luggage tag' })
    await user.type(screen.getByLabelText('Search designs'), 'key')
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Luggage tag' })).toBeNull())
    expect(screen.getByRole('heading', { name: 'Name keychain' })).toBeInTheDocument()
    server.events.removeAllListeners()
    expect(asked).toEqual(['', 'key'])
    await user.clear(screen.getByLabelText('Search designs'))
    await user.type(screen.getByLabelText('Search designs'), 'zzz')
    expect(await screen.findByText('No design matches “zzz”.')).toBeInTheDocument()
  })

  it('leaves a search the visitor left by the shop’s mark', async () => {
    function Where() {
      return <output data-testid="where">{useLocation().search}</output>
    }
    const { user } = renderPage(
      <>
        <Link to="/">Print shop</Link>
        <Where />
        <ListingsPage debounceMs={20} />
      </>,
    )
    await screen.findByRole('heading', { name: 'Luggage tag' })
    await user.type(screen.getByLabelText('Search designs'), 'key')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('?q=key'))
    await user.click(screen.getByRole('link', { name: 'Print shop' }))
    await screen.findByRole('heading', { name: 'Luggage tag' })
    expect(screen.getByLabelText('Search designs')).toHaveValue('')
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(screen.getByTestId('where')).toHaveTextContent(/^$/)
  })

  it('cuts a search longer than the edge takes', async () => {
    const asked: string[] = []
    server.events.on('request:start', ({ request }) => {
      const url = new URL(request.url)
      if (url.pathname === '/api/store/v1/listings') asked.push(url.searchParams.get('q') ?? '')
    })
    renderPage(<ListingsPage />, { route: `/?q=${'k'.repeat(150)}` })
    await screen.findByText(/No design matches/)
    server.events.removeAllListeners()
    expect(asked).toEqual(['k'.repeat(100)])
  })
})
