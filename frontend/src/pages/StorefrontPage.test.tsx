import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { resetMockState } from '../mocks/handlers'
import { FILAMENT_PLA } from '../mocks/features/orders/state'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { StorefrontPage } from './StorefrontPage'

const KEYCHAIN = 'name-keychain'
const COMMIT = 'c3d4e5f6a7b8'.repeat(4).slice(0, 40)

async function seed(title: string) {
  return api.createStorefrontListing({ slug: KEYCHAIN, commit: COMMIT, title })
}

/** Requests of `method` to `path` wait until the returned function is called, then fall through. */
function hold(method: 'post' | 'put' | 'delete', path: string): () => void {
  let release: () => void = () => undefined
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  server.use(
    http[method](path, async () => {
      await gate
      return undefined
    }),
  )
  return release
}

/** Each request the page sends, as `METHOD path`. */
function recordRequests(): string[] {
  const seen: string[] = []
  server.events.on('request:start', ({ request }) => {
    seen.push(`${request.method} ${new URL(request.url).pathname}`)
  })
  return seen
}

describe('StorefrontPage (#5282)', () => {
  beforeEach(() => resetMockState())

  it('locks a parameter and previews the customer view', async () => {
    const user = userEvent.setup()
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'New listing' }))
    // A new listing offers what the schema ranges: the text size among them.
    const preview = screen.getByTestId('listing-preview')
    expect(await within(preview).findByLabelText('Text size')).toBeInTheDocument()
    await user.click(await screen.findByRole('radio', { name: 'Locked at 14' }))
    expect(within(preview).queryByLabelText('Text size')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    expect(await screen.findByText('Published')).toBeInTheDocument()

    const [saved] = await api.storefrontListings()
    expect(saved?.published).toBe(true)
    expect(saved?.fixed['text_size']).toBe(14)
    expect(saved?.params['text_size']).toBeUndefined()
    expect(saved?.params['thickness']).toEqual({ kind: 'number', min: 2, max: 10, step: 0.2 })
    expect(saved?.title).toBe('Name Keychain')
  })

  it('shows the shop’s text as text, never as HTML', async () => {
    const user = userEvent.setup()
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'New listing' }))
    const description = await screen.findByLabelText('Description')
    await user.type(description, '<img src=x onerror=alert(1)>')
    const preview = screen.getByTestId('listing-preview')
    expect(within(preview).getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
    expect(preview.querySelector('img[src="x"]')).toBeNull()
  })

  it('orders the listings and unpublishes one', async () => {
    const user = userEvent.setup()
    const first = await seed('First')
    await seed('Second')
    await api.publishStorefrontListing(first.id)
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'Move Second up' }))
    await waitFor(async () => expect((await api.storefrontListings()).map((l) => l.title)).toEqual(['Second', 'First']))
    const rows = screen.getAllByTestId('storefront-listing')
    expect(rows.map((row) => within(row).getByRole('button', { name: /^Edit / }).textContent)).toEqual([
      'SecondNot published',
      'FirstPublished',
    ])

    await user.click(screen.getByRole('button', { name: 'Edit First' }))
    await user.click(await screen.findByRole('button', { name: 'Unpublish' }))
    await waitFor(() => expect(screen.queryByText('Published')).toBeNull())
    expect((await api.storefrontListings()).every((l) => !l.published)).toBe(true)
  })

  it('offers a colour parameter only once the listing has filaments', async () => {
    const user = userEvent.setup()
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'New listing' }))
    const plate = await screen.findByRole('group', { name: /body_color/ })
    expect(within(plate).getByRole('radio', { name: 'Customer may change' })).toBeDisabled()
    await user.click(await screen.findByRole('checkbox', { name: 'PLA Sunflower Yellow' }))
    await user.click(within(plate).getByRole('radio', { name: 'Customer may change' }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(async () => expect(await api.storefrontListings()).toHaveLength(1))
    const [saved] = await api.storefrontListings()
    expect(saved?.colours).toEqual([FILAMENT_PLA])
    expect(saved?.params['body_color']).toEqual({ kind: 'choice', choices: ['#F9D72C'] })
  })

  it('shows why a publish is refused', async () => {
    const user = userEvent.setup()
    const detail = "'name-keychain' is a pipeline template; the storefront cannot list one yet"
    server.use(
      http.post('/api/v1/storefront/listings/:id/publish', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/listing_pipeline_unsupported', title: 'Unprocessable Content', status: 422, detail },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'New listing' }))
    await within(screen.getByTestId('listing-preview')).findByLabelText('Text size')
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
    expect(screen.queryByText('Published')).toBeNull()
  })

  it('switches design requests on', async () => {
    const user = userEvent.setup()
    renderPage(<StorefrontPage />)
    const toggle = await screen.findByRole('switch', { name: 'Ask for a design' })
    await waitFor(() => expect(toggle).toBeEnabled())
    await user.click(toggle)
    await waitFor(async () => expect((await api.getSettings()).storefront_requests_enabled).toBe(true))
    expect(toggle).toBeChecked()
  })

  it('says why an edit of a listing changed elsewhere is refused', async () => {
    const user = userEvent.setup()
    const listing = await seed('Mine')
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'Edit Mine' }))
    await api.updateStorefrontListing(listing.id, { slug: KEYCHAIN, commit: COMMIT, title: 'Theirs' }, listing.updated_at)
    const title = await screen.findByLabelText('Title')
    await user.type(title, '!')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('changed since it was read')
  })

  it('keeps the listing opened while a publish of another was running', async () => {
    const user = userEvent.setup()
    await seed('First')
    await seed('Second')
    const release = hold('post', '/api/v1/storefront/listings/:id/publish')
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'Edit First' }))
    await user.click(await screen.findByRole('button', { name: 'Publish' }))
    await user.click(screen.getByRole('button', { name: 'Edit Second' }))
    expect(await screen.findByLabelText('Title')).toHaveValue('Second')
    release()
    await waitFor(async () => expect((await api.storefrontListings()).find((l) => l.title === 'First')?.published).toBe(true))
    await waitFor(() => expect(screen.getAllByText('Published')).toHaveLength(1))
    expect(screen.getByLabelText('Title')).toHaveValue('Second')
  })

  it('keeps the listing opened while a delete of another was running', async () => {
    const user = userEvent.setup()
    await seed('First')
    await seed('Second')
    const release = hold('delete', '/api/v1/storefront/listings/:id')
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'Edit First' }))
    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Edit Second' }))
    release()
    await waitFor(async () => expect(await api.storefrontListings()).toHaveLength(1))
    await waitFor(() => expect(screen.getAllByTestId('storefront-listing')).toHaveLength(1))
    expect(screen.getByLabelText('Title')).toHaveValue('Second')
  })

  it('shows the stored design-request switch when the save fails', async () => {
    const user = userEvent.setup()
    server.use(
      http.put('/api/v1/settings', () =>
        HttpResponse.json({ title: 'Forbidden', status: 403, detail: 'not allowed' }, { status: 403, headers: { 'Content-Type': 'application/problem+json' } }),
      ),
    )
    renderPage(<StorefrontPage />)
    const toggle = await screen.findByRole('switch', { name: 'Ask for a design' })
    await waitFor(() => expect(toggle).toBeEnabled())
    await user.click(toggle)
    expect(await screen.findByRole('alert')).toHaveTextContent('not allowed')
    expect(toggle).not.toBeChecked()
  })

  it('says to choose a filament for a colour parameter left with none', async () => {
    const user = userEvent.setup()
    const seen = recordRequests()
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'New listing' }))
    const plate = await screen.findByRole('group', { name: /body_color/ })
    const pla = await screen.findByRole('checkbox', { name: 'PLA Sunflower Yellow' })
    await user.click(pla)
    await user.click(within(plate).getByRole('radio', { name: 'Customer may change' }))
    await user.click(pla)
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/body_color.*choose at least one filament/)
    expect(seen.filter((r) => r.startsWith('POST /api/v1/storefront/listings'))).toEqual([])
  })

  it('publishes again, unchanged, without saving first', async () => {
    const user = userEvent.setup()
    const seen = recordRequests()
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'New listing' }))
    await within(screen.getByTestId('listing-preview')).findByLabelText('Text size')
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    expect(await screen.findByText('Published')).toBeInTheDocument()
    const [listing] = await api.storefrontListings()
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    await waitFor(() => expect(seen.filter((r) => r === `POST /api/v1/storefront/listings/${listing?.id}/publish`)).toHaveLength(2))
    expect(seen.filter((r) => r.startsWith('PUT /api/v1/storefront/listings/'))).toEqual([])
  })

  it('publishes an unchanged published listing again without taking it down first', async () => {
    const user = userEvent.setup()
    const listing = await seed('Live')
    await api.publishStorefrontListing(listing.id)
    const seen = recordRequests()
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'Edit Live' }))
    await within(screen.getByTestId('listing-preview')).findByText('Live')
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    await waitFor(() => expect(seen).toContain(`POST /api/v1/storefront/listings/${listing.id}/publish`))
    expect(seen.filter((r) => r.startsWith('PUT /api/v1/storefront/listings/'))).toEqual([])
  })
})
