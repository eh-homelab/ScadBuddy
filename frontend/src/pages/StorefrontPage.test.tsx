import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { resetMockState } from '../mocks/handlers'
import { keychainSchema, versionIds } from '../mocks/fixtures'
import { FILAMENT_PETG, FILAMENT_PLA } from '../mocks/features/orders/state'
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

  it('starts a new listing with the smart defaults, and previews the customer view', async () => {
    const user = userEvent.setup()
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'New listing' }))
    // #6131 — free text is the customer's; the geometry is the shop's.
    const preview = screen.getByTestId('listing-preview')
    expect(await within(preview).findByLabelText('Name on the tag')).toBeInTheDocument()
    expect(within(preview).queryByLabelText('Text size')).toBeNull()
    await user.click(screen.getByRole('button', { name: /^Show all/ }))
    await user.click(screen.getByRole('switch', { name: 'Customer can change Text size' }))
    expect(await within(preview).findByLabelText('Text size')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    expect(await screen.findByText('Published')).toBeInTheDocument()

    const [saved] = await api.storefrontListings()
    expect(saved?.published).toBe(true)
    expect(saved?.params['text_size']).toEqual({ kind: 'number', min: 6, max: 28, step: 0.5 })
    expect(saved?.params['name']).toEqual({ kind: 'text', max_length: 20 })
    expect(saved?.fixed['thickness']).toBe(5.2)
    expect(saved?.title).toBe('Name Keychain')
    // The template's picture is shown.
    expect(saved?.media_ids).toEqual(['thumbnail'])
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
    await user.click(await screen.findByRole('button', { name: /^Show all/ }))
    const offer = await screen.findByRole('switch', { name: 'Customer can change Plate' })
    expect(offer).toBeDisabled()
    expect(screen.getAllByText('Tick a colour on offer first.').length).toBeGreaterThan(0)
    await user.click(await screen.findByRole('checkbox', { name: 'PLA Sunflower Yellow' }))
    await user.click(screen.getByRole('switch', { name: 'Customer can change Plate' }))
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
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
    await within(screen.getByTestId('listing-preview')).findByLabelText('Name on the tag')
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
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
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
    const pla = await screen.findByRole('checkbox', { name: 'PLA Sunflower Yellow' })
    await user.click(pla)
    await user.click(await screen.findByRole('button', { name: /^Show all/ }))
    await user.click(screen.getByRole('switch', { name: 'Customer can change Plate' }))
    await user.click(pla)
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/body_color.*choose at least one filament/)
    expect(seen.filter((r) => r.startsWith('POST /api/v1/storefront/listings'))).toEqual([])
  })

  it('publishes again, unchanged, without saving first', async () => {
    const user = userEvent.setup()
    const seen = recordRequests()
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'New listing' }))
    await within(screen.getByTestId('listing-preview')).findByLabelText('Name on the tag')
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

  it('shows the listings again after a refused order', async () => {
    const user = userEvent.setup()
    await seed('First')
    await seed('Second')
    server.use(
      http.put('/api/v1/storefront/listings/order', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/listing_changed', title: 'Conflict', status: 409, detail: 'the order must name every listing once; load the listings again' },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    renderPage(<StorefrontPage />)
    await screen.findByRole('button', { name: 'Edit Second' })
    // Another tab adds a listing.
    await seed('Third')
    await user.click(screen.getByRole('button', { name: 'Move Second up' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('load the listings again')
    await waitFor(() =>
      expect(screen.getAllByTestId('storefront-listing').map((row) => within(row).getByRole('button', { name: /^Edit / }).getAttribute('aria-label'))).toEqual([
        'Edit First',
        'Edit Second',
        'Edit Third',
      ]),
    )
  })

  it('drops the parameters a newly chosen version does not have', async () => {
    const user = userEvent.setup()
    const older = versionIds.edited
    server.use(
      http.get('/api/v1/models/:slug/versions/:commit/schema', ({ params }) =>
        params['commit'] === older
          ? HttpResponse.json({ ...keychainSchema, parameters: (keychainSchema.parameters ?? []).filter((p) => p.name !== 'text_size' && p.name !== 'padding') })
          : undefined,
      ),
    )
    await api.createStorefrontListing({
      slug: KEYCHAIN,
      commit: versionIds.raised,
      title: 'Tag',
      params: { text_size: { kind: 'number', min: 6, max: 20, step: null } },
      fixed: { padding: 6, corner_radius: 4 },
    })
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'Edit Tag' }))
    await user.click(await screen.findByRole('button', { name: 'Change' }))
    await user.click(await screen.findByRole('combobox', { name: 'Version' }))
    await user.click(await screen.findByRole('option', { name: /Update name-keychain metadata/ }))
    await user.click(await screen.findByRole('button', { name: /^Show all/ }))
    await waitFor(() => expect(screen.queryByRole('group', { name: /Text size/ })).toBeNull())
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(async () => expect((await api.storefrontListings())[0]?.commit).toBe(older))
    const [saved] = await api.storefrontListings()
    expect(saved?.params).toEqual({})
    expect(saved?.fixed).toEqual({ corner_radius: 4 })
  })

  it('takes no drag while an order is being saved', async () => {
    const user = userEvent.setup()
    await seed('First')
    await seed('Second')
    await seed('Third')
    const seen = recordRequests()
    const release = hold('put', '/api/v1/storefront/listings/order')
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'Move Second up' }))
    const rows = screen.getAllByTestId('storefront-listing')
    fireEvent.dragStart(rows[2]!)
    fireEvent.drop(rows[0]!)
    release()
    await waitFor(async () => expect((await api.storefrontListings()).map((l) => l.title)).toEqual(['Second', 'First', 'Third']))
    expect(seen.filter((r) => r === 'PUT /api/v1/storefront/listings/order')).toHaveLength(1)
  })

  it('shows a refused publish only on its own listing, and leaves another free', async () => {
    const user = userEvent.setup()
    await seed('First')
    await seed('Second')
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.post('/api/v1/storefront/listings/:id/publish', async () => {
        await gate
        return HttpResponse.json(
          { title: 'Unprocessable Content', status: 422, detail: 'refused for First' },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
    )
    renderPage(<StorefrontPage />)
    await user.click(await screen.findByRole('button', { name: 'Edit First' }))
    await user.click(await screen.findByRole('button', { name: 'Publish' }))
    await user.click(screen.getByRole('button', { name: 'Edit Second' }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('Second'))
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeEnabled()
    release()
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  describe('Sell on storefront (#6131)', () => {
    /** A render of the keychain the person changed: their name, a bigger text, a yellow plate, slow. */
    function serveJob(over: Record<string, unknown> = {}) {
      server.use(
        http.get('/api/v1/jobs/:id', ({ params }) =>
          HttpResponse.json({
            id: params['id'],
            slug: KEYCHAIN,
            status: 'done',
            model_version: versionIds.edited,
            params: { name: 'Mum', text_size: 20, body_color: '#f9d72c', text_color: '#E8532F' },
            inputs: {},
            created_at: '2026-11-13T10:00:00Z',
            started_at: '2026-11-13T10:00:00Z',
            finished_at: '2026-11-13T10:02:30Z',
            diagnostics: [],
            outputs: [],
            log_tail: [],
            ...over,
          }),
        ),
      )
    }

    it('opens the listing filled in from a render, unsaved, and publishes it once its colours are fixed', async () => {
      serveJob()
      const seen = recordRequests()
      const { user } = renderPage(<StorefrontPage />, { route: '/storefront?sell=job:j1' })
      expect(await screen.findByLabelText('Title')).toHaveValue('Name Keychain')
      // The design and the version the render read.
      expect(await screen.findByTestId('listing-design')).toHaveTextContent(`Name Keychainversion ${versionIds.edited.slice(0, 7)}Change`)
      // The slow render is said before publishing (#6120).
      expect(screen.getByRole('note')).toHaveTextContent('took 150 s to render')
      // The changed colour is locked at its filament; the text colour has none.
      const problems = await screen.findByTestId('colour-problems')
      expect(problems).toHaveTextContent('Text is #E8532F')
      expect(screen.getByRole('checkbox', { name: 'PLA Sunflower Yellow' })).toBeChecked()
      expect(seen.filter((r) => r.startsWith('POST /api/v1/storefront/listings'))).toEqual([])

      await user.click(within(problems).getByRole('combobox', { name: 'Filament for Text' }))
      await user.click(await screen.findByRole('option', { name: 'PETG Black' }))
      await user.click(screen.getByRole('button', { name: 'Publish' }))
      expect(await screen.findByText('Published')).toBeInTheDocument()

      const [saved] = await api.storefrontListings()
      expect(saved?.commit).toBe(versionIds.edited)
      expect(saved?.params).toEqual({ name: { kind: 'text', max_length: 20 } })
      expect(saved?.fixed).toMatchObject({ text_size: 20, body_color: '#f9d72c', text_color: '#1E1E1E', thickness: 5.2 })
      expect(saved?.colours).toEqual([FILAMENT_PLA, FILAMENT_PETG])
      expect(saved?.media_ids).toEqual(['thumbnail'])
    })

    it('takes the output’s name for a saved output, and the current version for one outside the history', async () => {
      serveJob({ model_version: 'f'.repeat(40) })
      server.use(
        http.get('/api/v1/outputs/:id', ({ params }) =>
          HttpResponse.json({
            id: params['id'],
            slug: KEYCHAIN,
            model_version: 'f'.repeat(40),
            name: 'Navy tag',
            job_id: 'j2',
            created_at: '2026-11-13T10:00:00Z',
            bbox_mm: { x: 1, y: 1, z: 1 },
            params: { name: 'Mum' },
            has_thumbnail: false,
          }),
        ),
      )
      renderPage(<StorefrontPage />, { route: '/storefront?sell=output:o1' })
      expect(await screen.findByLabelText('Title')).toHaveValue('Navy tag')
      expect(await screen.findByTestId('listing-design')).toHaveTextContent(`version ${versionIds.raised.slice(0, 7)} (current)`)
      expect(screen.getAllByRole('note')[0]).toHaveTextContent('not in the design')
    })

    it('says why a listing could not be filled in', async () => {
      server.use(
        http.get('/api/v1/jobs/:id', () =>
          HttpResponse.json({ title: 'Not Found', status: 404, detail: 'no such job' }, { status: 404, headers: { 'Content-Type': 'application/problem+json' } }),
        ),
      )
      renderPage(<StorefrontPage />, { route: '/storefront?sell=job:gone' })
      expect(await screen.findByRole('alert')).toHaveTextContent('Could not fill in the listing: no such job')
    })

    it('opens the listing the assistant names', async () => {
      const listing = await seed('From the assistant')
      renderPage(<StorefrontPage />, { route: `/storefront?listing=${listing.id}` })
      expect(await screen.findByLabelText('Title')).toHaveValue('From the assistant')
    })

    it('warns that saving a published listing takes it off the store', async () => {
      const listing = await seed('Live')
      await api.publishStorefrontListing(listing.id)
      const { user } = renderPage(<StorefrontPage />)
      await user.click(await screen.findByRole('button', { name: 'Edit Live' }))
      const title = await screen.findByLabelText('Title')
      expect(screen.queryByText(/takes this listing off the store/)).toBeNull()
      await user.type(title, '!')
      expect(screen.getByText(/takes this listing off the store/)).toBeInTheDocument()
    })

    it('says why Publish waits for a title', async () => {
      const { user } = renderPage(<StorefrontPage />)
      await user.click(await screen.findByRole('button', { name: 'New listing' }))
      await user.clear(await screen.findByLabelText('Title'))
      expect(screen.getByText('Give the product a title to publish.')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Publish' })).toBeDisabled()
    })
  })
})
