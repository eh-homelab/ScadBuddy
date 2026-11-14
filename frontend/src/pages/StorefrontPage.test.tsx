import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Route, Routes, useLocation } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../api/client'
import type { StorefrontListing } from '../api/types'
import { resetMockState } from '../mocks/handlers'
import { FILAMENT_PETG } from '../mocks/features/orders/state'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { StorefrontListingPage } from './StorefrontListingPage'
import { StorefrontPage } from './StorefrontPage'

const KEYCHAIN = 'name-keychain'
const COMMIT = 'c3d4e5f6a7b8'.repeat(4).slice(0, 40)

async function seed(title: string, extra: { slug?: string; colours?: string[] } = {}) {
  return api.createStorefrontListing({ slug: KEYCHAIN, commit: COMMIT, title, ...extra })
}

/** Every listing, read whole, in the shop's order. */
async function stored(): Promise<StorefrontListing[]> {
  const page = await api.storefrontListings(new URLSearchParams({ limit: '200' }))
  return Promise.all(page.items.map((row) => api.storefrontListing(row.id)))
}

/** The address the router is at, for a test to read. */
function Where() {
  const location = useLocation()
  return <span data-testid="where">{location.pathname + location.search}</span>
}

function renderStorefront(route = '/storefront') {
  return renderPage(
    <>
      <Routes>
        <Route path="/storefront" element={<StorefrontPage />} />
        <Route path="/storefront/listings/:id" element={<StorefrontListingPage />} />
      </Routes>
      <Where />
    </>,
    { route },
  )
}

/** The titles of the rows shown, in order. */
function titles(): string[] {
  return screen.getAllByTestId('storefront-listing').map((row) => within(row).getAllByRole('link')[0]?.textContent ?? '')
}

describe('StorefrontPage (#5282, #6157)', () => {
  beforeEach(() => resetMockState())

  it('lists the listings in a table, 50 to a page, and pages through them', async () => {
    for (let i = 1; i <= 55; i += 1) await seed(`Listing ${i}`)
    const { user } = renderStorefront()
    await waitFor(() => expect(screen.getAllByTestId('storefront-listing')).toHaveLength(50))
    expect(screen.getByTestId('storefront-range')).toHaveTextContent('1–50 of 55')
    await user.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() => expect(titles()).toEqual(['Listing 51', 'Listing 52', 'Listing 53', 'Listing 54', 'Listing 55']))
    expect(screen.getByTestId('where')).toHaveTextContent('/storefront?page=2')
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
  })

  it('searches, filters and sorts on the server, and keeps it in the address', async () => {
    await seed('Owl cup')
    await seed('Star tag', { colours: [FILAMENT_PETG] })
    await seed('Owl tag', { slug: 'gridfinity-bin' })
    const { user } = renderStorefront()
    await screen.findByRole('link', { name: 'Owl cup' })
    await user.type(screen.getByRole('searchbox', { name: 'Search title or model' }), 'owl')
    await waitFor(() => expect(titles()).toEqual(['Owl cup', 'Owl tag']))
    expect(screen.getByTestId('where')).toHaveTextContent('/storefront?q=owl')
    // The model is a filter too; "gridfinity" matches the slug.
    await user.clear(screen.getByRole('searchbox', { name: 'Search title or model' }))
    await user.type(screen.getByRole('searchbox', { name: 'Search title or model' }), 'gridfinity')
    await waitFor(() => expect(titles()).toEqual(['Owl tag']))
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    await waitFor(() => expect(titles()).toHaveLength(3))
    // Sort by title, then the other way.
    await user.click(screen.getByRole('button', { name: 'Title' }))
    await waitFor(() => expect(titles()).toEqual(['Owl cup', 'Owl tag', 'Star tag']))
    await user.click(screen.getByRole('button', { name: 'Title' }))
    await waitFor(() => expect(titles()).toEqual(['Star tag', 'Owl tag', 'Owl cup']))
    expect(screen.getByTestId('where')).toHaveTextContent('sort=title&dir=desc')
  })

  it('reads its search and filters from the address', async () => {
    await seed('Owl cup')
    await seed('Star tag', { colours: [FILAMENT_PETG] })
    renderStorefront('/storefront?material=PETG')
    await waitFor(() => expect(titles()).toEqual(['Star tag']))
  })

  it('says when nothing matches, and offers to clear the filters', async () => {
    await seed('Owl cup')
    const { user } = renderStorefront('/storefront?q=zebra')
    expect(await screen.findByText(/No listing matches/)).toBeInTheDocument()
    await user.click(screen.getAllByRole('button', { name: 'Clear filters' }).at(-1)!)
    expect(await screen.findByRole('link', { name: 'Owl cup' })).toBeInTheDocument()
  })

  it('says how to start when there are no listings', async () => {
    renderStorefront()
    expect(await screen.findByText(/No listings yet/)).toBeInTheDocument()
  })

  it('says why the listings did not load, and tries again', async () => {
    server.use(
      http.get(
        '/api/v1/storefront/listings',
        () => HttpResponse.json({ title: 'Service Unavailable', status: 503, detail: 'database down' }, { status: 503, headers: { 'Content-Type': 'application/problem+json' } }),
        { once: true },
      ),
    )
    await seed('Back again')
    const { user } = renderStorefront()
    expect(await screen.findByRole('alert')).toHaveTextContent('database down')
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('link', { name: 'Back again' })).toBeInTheDocument()
  })

  it('publishes, unpublishes and deletes the selected listings, one at a time', async () => {
    for (const title of ['A', 'B', 'C']) await seed(title)
    const { user } = renderStorefront()
    await screen.findByRole('link', { name: 'A' })
    await user.click(screen.getByRole('checkbox', { name: 'Select A' }))
    await user.click(screen.getByRole('checkbox', { name: 'Select C' }))
    await user.click(within(screen.getByRole('region', { name: 'Selected listings' })).getByRole('button', { name: 'Publish' }))
    await waitFor(async () => expect((await stored()).map((l) => l.published)).toEqual([true, false, true]))
    expect(await screen.findByText('Published 2 of 2.')).toBeInTheDocument()

    // A published listing is refused a delete; the others go, and the refusal is listed.
    await user.click(screen.getByRole('checkbox', { name: 'Select every listing on this page' }))
    await user.click(within(screen.getByRole('region', { name: 'Selected listings' })).getByRole('button', { name: 'Delete' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }))
    await waitFor(async () => expect((await stored()).map((l) => l.title)).toEqual(['A', 'C']))
    expect(await screen.findByText(/Deleted 1 of 3\. 2 refused\./)).toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'Refused' })).getAllByRole('listitem')).toHaveLength(2)
    // The refused ones stay selected, for an Unpublish.
    await user.click(within(screen.getByRole('region', { name: 'Selected listings' })).getByRole('button', { name: 'Unpublish' }))
    await waitFor(async () => expect((await stored()).every((l) => !l.published)).toBe(true))
  })

  it('moves a listing to the top, or to a place, by one request each', async () => {
    for (const title of ['A', 'B', 'C', 'D']) await seed(title)
    const { user } = renderStorefront()
    await screen.findByRole('link', { name: 'D' })
    await user.click(screen.getByRole('button', { name: 'Actions for D' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Move to top' }))
    await waitFor(() => expect(titles()).toEqual(['D', 'A', 'B', 'C']))
    await user.click(screen.getByRole('button', { name: 'Actions for D' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Move to position…' }))
    const place = await screen.findByLabelText(/New place/)
    await user.clear(place)
    await user.type(place, '3{Enter}')
    await waitFor(() => expect(titles()).toEqual(['A', 'B', 'D', 'C']))
    expect((await stored()).map((l) => l.title)).toEqual(['A', 'B', 'D', 'C'])
  })

  it('moves a listing found by a search to a place past the matches (#6157 review)', async () => {
    for (const title of ['A', 'B', 'C', 'Owl']) await seed(title)
    const { user } = renderStorefront('/storefront?q=owl')
    await screen.findByRole('link', { name: 'Owl' })
    await user.click(screen.getByRole('button', { name: 'Actions for Owl' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Move to position…' }))
    // One match, but four listings: the places are the shop's.
    const place = await screen.findByLabelText('New place in the shop (1 to 4)')
    await user.clear(place)
    await user.type(place, '2{Enter}')
    await waitFor(async () => expect((await stored()).map((l) => l.title)).toEqual(['A', 'Owl', 'B', 'C']))
  })

  it('moves to the last page when the page asked for is past it (#6157 review)', async () => {
    for (let i = 1; i <= 3; i += 1) await seed(`Listing ${i}`)
    renderStorefront('/storefront?page=4')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(/^\/storefront$/))
    await waitFor(() => expect(titles()).toEqual(['Listing 1', 'Listing 2', 'Listing 3']))
    expect(screen.getByTestId('storefront-range')).toHaveTextContent('1–3 of 3')
  })

  it('puts the cursor in the search on "/"', async () => {
    await seed('Owl cup')
    const { user } = renderStorefront()
    await screen.findByRole('link', { name: 'Owl cup' })
    await user.keyboard('/')
    expect(screen.getByRole('searchbox', { name: 'Search title or model' })).toHaveFocus()
  })

  it('switches design requests on', async () => {
    const { user } = renderStorefront()
    const toggle = await screen.findByRole('switch', { name: 'Ask for a design' })
    await waitFor(() => expect(toggle).toBeEnabled())
    await user.click(toggle)
    await waitFor(async () => expect((await api.getSettings()).storefront_requests_enabled).toBe(true))
    expect(toggle).toBeChecked()
  })

  it('shows the stored design-request switch when the save fails', async () => {
    server.use(
      http.put('/api/v1/settings', () =>
        HttpResponse.json({ title: 'Forbidden', status: 403, detail: 'not allowed' }, { status: 403, headers: { 'Content-Type': 'application/problem+json' } }),
      ),
    )
    const { user } = renderStorefront()
    const toggle = await screen.findByRole('switch', { name: 'Ask for a design' })
    await waitFor(() => expect(toggle).toBeEnabled())
    await user.click(toggle)
    expect(await screen.findByRole('alert')).toHaveTextContent('not allowed')
    expect(toggle).not.toBeChecked()
  })

  it('opens New listing on its own address', async () => {
    const { user } = renderStorefront()
    await user.click(await screen.findByRole('link', { name: 'New listing' }))
    expect(await screen.findByRole('heading', { name: 'New listing' })).toBeInTheDocument()
    expect(screen.getByTestId('where')).toHaveTextContent('/storefront/listings/new')
  })
})
