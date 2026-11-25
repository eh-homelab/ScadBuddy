import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Route, Routes, useLocation, useNavigate } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { MediaView, StorefrontListing } from '../api/types'
import { resetMockState, setMockMedia } from '../mocks/handlers'
import { DOLLHOUSE_SLUG, DOLLHOUSE_VERSION, keychainSchema, versionIds } from '../mocks/fixtures'
import { FILAMENT_PETG, FILAMENT_PLA } from '../mocks/features/orders/state'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { StorefrontListingPage } from './StorefrontListingPage'
import { StorefrontPage } from './StorefrontPage'

const KEYCHAIN = 'name-keychain'
const COMMIT = 'c3d4e5f6a7b8'.repeat(4).slice(0, 40)

async function seed(title: string) {
  return api.createStorefrontListing({ slug: KEYCHAIN, commit: COMMIT, title })
}

/** Every listing, read whole, in the shop's order. */
async function stored(): Promise<StorefrontListing[]> {
  const page = await api.storefrontListings(new URLSearchParams({ limit: '200' }))
  return Promise.all(page.items.map((row) => api.storefrontListing(row.id)))
}

/** Requests of `method` to `path` wait until the returned function is called, then fall through. */
function hold(method: 'get' | 'post' | 'put' | 'delete', path: string): () => void {
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

/** A button that moves the router to `to` in the same tree, as an in-app link does. */
function GoTo({ to }: { to: string }) {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => void navigate(to)}>
      Go
    </button>
  )
}

/** The router's path, shown for a test to read. */
function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>
}

/** Each request the page sends, as `METHOD path`. */
function recordRequests(): string[] {
  const seen: string[] = []
  server.events.on('request:start', ({ request }) => {
    seen.push(`${request.method} ${new URL(request.url).pathname}`)
  })
  return seen
}

/** The list and the editor at their app routes, starting at `route`. */
function renderStorefront(route = '/storefront/listings/new', extra?: React.ReactNode) {
  return renderPage(
    <>
      <Routes>
        <Route path="/storefront" element={<StorefrontPage />} />
        <Route path="/storefront/listings/:id" element={<StorefrontListingPage />} />
      </Routes>
      {extra}
    </>,
    { route },
  )
}

describe('StorefrontListingPage (#5282, #6157)', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it('starts a new listing with the smart defaults, and previews the customer view', async () => {
    const { user } = renderStorefront()
    // #6131 — free text is the customer's; the geometry is the shop's.
    const preview = await screen.findByTestId('listing-preview')
    expect(await within(preview).findByLabelText('Name on the tag')).toBeInTheDocument()
    expect(within(preview).queryByLabelText('Text size')).toBeNull()
    await user.click(screen.getByRole('switch', { name: 'Customer can change Text size' }))
    expect(await within(preview).findByLabelText('Text size')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    expect(await screen.findByText('Published')).toBeInTheDocument()

    const [saved] = await stored()
    expect(saved?.published).toBe(true)
    expect(saved?.params['text_size']).toEqual({ kind: 'number', min: 6, max: 28, step: 0.5, label: null, default: null })
    expect(saved?.params['name']).toEqual({ kind: 'text', max_length: 20, label: null, default: null })
    expect(saved?.fixed['thickness']).toBe(5.2)
    expect(saved?.title).toBe('Name Keychain')
    // The template's picture is shown.
    expect(saved?.media_ids).toEqual(['thumbnail'])
  })

  it('opens from the list, keeps its own address, and goes back to the same page of the list', async () => {
    for (const title of ['First', 'Second']) await seed(title)
    const { user } = renderStorefront('/storefront?q=sec')
    await user.click(await screen.findByRole('link', { name: 'Second' }))
    expect(await screen.findByLabelText('Title')).toHaveValue('Second')
    const title = screen.getByLabelText('Title')
    await user.type(title, '!')
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    // A save stays on the editor.
    await waitFor(() => expect(screen.queryByText('Unsaved changes')).toBeNull())
    expect(screen.getByLabelText('Title')).toHaveValue('Second!')
    await user.click(screen.getByRole('link', { name: 'Back to listings' }))
    // The list's search is kept.
    expect(await screen.findByRole('link', { name: 'Second!' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'First' })).toBeNull()
    expect(screen.getByRole('searchbox', { name: 'Search title or model' })).toHaveValue('sec')
  })

  it('moves a new listing’s address to its id at its first save', async () => {
    const seen = recordRequests()
    const { user } = renderStorefront('/storefront/listings/new', <Where />)
    await within(await screen.findByTestId('listing-preview')).findByLabelText('Name on the tag')
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(async () => expect(await stored()).toHaveLength(1))
    const [saved] = await stored()
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(`/storefront/listings/${saved?.id}`))
    // The next save edits it: no second create.
    await user.type(screen.getByLabelText('Title'), '!')
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(seen).toContain(`PUT /api/v1/storefront/listings/${saved?.id}`))
    expect(await stored()).toHaveLength(1)
  })

  it('says a listing that is gone is gone', async () => {
    renderStorefront('/storefront/listings/nope')
    expect(await screen.findByRole('alert')).toHaveTextContent('That listing is gone.')
  })

  describe('a library file as the source (#6710 §9)', () => {
    /** A file listing saved, and published when asked, from the mock's library file `id`. */
    async function seedFile(id = 46, publish = true) {
      const saved = await api.createStorefrontListing({
        source: 'library_file',
        library_file: { id, library: 'bambuddy' },
        title: 'Desiccant box',
        colours: [FILAMENT_PLA],
        media_ids: [],
      })
      return publish ? api.publishStorefrontListing(saved.id) : saved
    }

    it('makes a listing of a library file: its name as the title, its plate pictures, its parts, then publishes it', async () => {
      const { user } = renderStorefront()
      await user.click(await screen.findByRole('radio', { name: 'A library file' }))
      await user.type(screen.getByLabelText('Library file'), 'drawer')
      await user.click(await screen.findByRole('button', { name: /^drawer-label\.3mf/ }))
      await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('drawer-label'))
      expect(await within(screen.getByRole('region', { name: 'Parts' })).findAllByTestId('listing-part')).toHaveLength(2)
      await waitFor(() => expect(within(screen.getByRole('group', { name: 'Pictures' })).getByRole('img', { name: 'Plate 1' })).toBeInTheDocument())
      await user.click(screen.getByRole('button', { name: 'Publish' }))
      expect(await screen.findByText('Published')).toBeInTheDocument()
      const [saved] = await stored()
      expect(saved).toMatchObject({ source: 'library_file', slug: null, commit: null, media_ids: ['plate:1'], published: true })
      expect(saved?.library_file).toMatchObject({ id: 120, library: 'bambuddy', repin: false })
      expect(saved?.library_file?.pinned).toMatch(/^[0-9a-f]{64}$/)
      expect(saved?.colours.length).toBeGreaterThan(0)
    })

    it('starts with every plate picture when the plates answer after the file and its parts (#7092 review)', async () => {
      const release = hold('get', '/api/v1/print/library/:id/plates')
      const { user } = renderStorefront('/storefront/listings/new?sell=library:120')
      expect(await within(await screen.findByRole('region', { name: 'Parts' })).findAllByTestId('listing-part')).toHaveLength(2)
      await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('drawer-label'))
      release()
      await waitFor(() => expect(within(screen.getByRole('group', { name: 'Pictures' })).getByRole('img', { name: 'Plate 1' })).toBeInTheDocument())
      await user.click(screen.getByRole('button', { name: 'Save draft' }))
      await waitFor(async () => expect((await stored())[0]?.media_ids).toEqual(['plate:1']))
    })

    it('starts with no pictures when the plates cannot be read, and fills in the rest', async () => {
      server.use(http.get('/api/v1/print/library/:id/plates', () => HttpResponse.json({ detail: 'down' }, { status: 502 })))
      renderStorefront('/storefront/listings/new?sell=library:120')
      await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('drawer-label'))
      await waitFor(() => expect(within(screen.getByRole('group', { name: 'Colours on offer' })).getByText(/ticked/)).toBeInTheDocument())
      expect(within(screen.getByRole('group', { name: 'Pictures' })).getByText('No pictures: the customer sees none.')).toBeInTheDocument()
    })

    it('refuses to list a library file when the library that serves cannot be read (#7092 review)', async () => {
      server.use(http.get('/api/v1/settings', () => HttpResponse.json({ detail: 'down' }, { status: 502 })))
      renderStorefront('/storefront/listings/new?sell=library:120')
      expect(await screen.findByRole('alert')).toHaveTextContent('Could not fill in the listing: the settings could not be read, so the library that serves is not known. Reload the page to try again')
      expect(await stored()).toEqual([])
    })

    it('holds Publish and Save draft while an upload saves the new listing (#7092 review)', async () => {
      const release = hold('post', '/api/v1/storefront/listings')
      const { user } = renderStorefront('/storefront/listings/new?sell=library:120')
      await waitFor(() => expect(within(screen.getByRole('group', { name: 'Pictures' })).getByRole('img', { name: 'Plate 1' })).toBeInTheDocument())
      await user.upload(screen.getByLabelText('Upload a picture'), new File([new Uint8Array([137, 80, 78, 71])], 'photo.png', { type: 'image/png' }))
      await waitFor(() => expect(screen.getByRole('button', { name: 'Publish' })).toBeDisabled())
      expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
      release()
      await waitFor(() => expect(screen.getByRole('button', { name: 'Publish' })).toBeEnabled())
      expect(await stored()).toHaveLength(1)
    })

    it('keeps the published version’s parts after a save, when its file is gone (#7092 review)', async () => {
      const listing = await seedFile()
      server.use(http.get('/api/v1/print/library/46*', () => HttpResponse.json({ detail: 'gone' }, { status: 404 })))
      const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
      expect(await within(await screen.findByRole('region', { name: 'Parts' })).findAllByTestId('listing-part')).toHaveLength(2)
      await user.type(screen.getByLabelText('Title'), '!')
      await user.click(screen.getByRole('button', { name: 'Save draft' }))
      await waitFor(() => expect(screen.queryByText('Unsaved changes')).toBeNull())
      expect(within(screen.getByRole('region', { name: 'Parts' })).getAllByTestId('listing-part')).toHaveLength(2)
    })

    it('saves the listing as it is when Save is clicked, after the file’s reads finished under the question (#7092 review)', async () => {
      const listing = await seedFile()
      const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
      await user.click(within(await screen.findByTestId('listing-source')).getByRole('button', { name: 'Change' }))
      await user.click(within(screen.getByTestId('listing-file')).getByRole('button', { name: 'Change' }))
      const release = hold('get', '/api/v1/print/library/:id/plates')
      await user.type(screen.getByLabelText('Library file'), 'drawer')
      await user.click(await screen.findByRole('button', { name: /^drawer-label\.3mf/ }))
      await user.click(screen.getByRole('button', { name: 'Save draft' }))
      const ask = await screen.findByRole('alertdialog')
      release()
      // The question holds the page modal; the defaults fill in behind it.
      await waitFor(() => expect(within(screen.getByRole('group', { name: 'Pictures', hidden: true })).getByRole('img', { name: 'Plate 1', hidden: true })).toBeInTheDocument())
      await user.click(within(ask).getByRole('button', { name: 'Save' }))
      await waitFor(async () => expect((await stored())[0]).toMatchObject({ library_file: { id: 120 }, media_ids: ['plate:1'] }))
      await waitFor(() => expect(screen.queryByText('Unsaved changes')).toBeNull())
    })

    it('asks before a pinned file listing is saved as a design (#7092 review)', async () => {
      const listing = await seedFile()
      const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
      await user.click(within(await screen.findByTestId('listing-source')).getByRole('button', { name: 'Change' }))
      await user.click(screen.getByRole('radio', { name: 'A design' }))
      await user.click(screen.getByRole('combobox', { name: 'Design' }))
      await user.click(await screen.findByRole('option', { name: 'Name Keychain' }))
      await waitFor(() => expect(screen.getByRole('button', { name: 'Save draft' })).toBeEnabled())
      await user.click(screen.getByRole('button', { name: 'Save draft' }))
      expect(await screen.findByRole('alertdialog')).toHaveTextContent('Customers lose the version you published. The next publish sells the design you picked.')
    })

    it('brings back the stored file’s pictures when it is picked again (#7092 review)', async () => {
      const saved = await seedFile(46, false)
      await api.updateStorefrontListing(saved.id, { source: 'library_file', library_file: { id: 46, library: 'bambuddy' }, title: 'Desiccant box', colours: [FILAMENT_PLA], media_ids: ['upload:' + 'c'.repeat(64)] }, saved.updated_at)
      const listing = await api.publishStorefrontListing(saved.id)
      const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
      await user.click(within(await screen.findByTestId('listing-source')).getByRole('button', { name: 'Change' }))
      const change = async (query: string, name: RegExp) => {
        await user.click(within(screen.getByTestId('listing-file')).getByRole('button', { name: 'Change' }))
        await user.type(screen.getByLabelText('Library file'), query)
        await user.click(await screen.findByRole('button', { name }))
      }
      await change('drawer', /^drawer-label\.3mf/)
      await waitFor(() => expect(within(screen.getByRole('group', { name: 'Pictures' })).getByRole('img', { name: 'Plate 1' })).toBeInTheDocument())
      await change('desiccant', /^Desiccant_Box\.stl/)
      expect(within(screen.getByRole('group', { name: 'Pictures' })).getByText('Uploaded picture')).toBeInTheDocument()
      expect(within(screen.getByRole('list', { name: 'Pictures shown' })).getAllByTestId('listing-picture')).toHaveLength(1)
      expect(screen.queryByText('Unsaved changes')).toBeNull()
    })

    it('fills the colours and plate pictures in after an upload saved the new listing first (#7092 review)', async () => {
      const release = hold('get', '/api/v1/print/library/:id/filaments')
      const { user } = renderStorefront('/storefront/listings/new?sell=library:120')
      await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('drawer-label'))
      await user.upload(screen.getByLabelText('Upload a picture'), new File([new Uint8Array([137, 80, 78, 71])], 'photo.png', { type: 'image/png' }))
      await waitFor(() => expect(within(screen.getByRole('group', { name: 'Pictures' })).getByText('Uploaded picture')).toBeInTheDocument())
      release()
      await waitFor(() => expect(within(screen.getByRole('group', { name: 'Pictures' })).getByRole('img', { name: 'Plate 1' })).toBeInTheDocument())
      await user.click(screen.getByRole('button', { name: 'Save draft' }))
      await waitFor(async () => expect((await stored())[0]?.media_ids).toEqual(['plate:1', expect.stringMatching(/^upload:/)]))
      expect((await stored())[0]?.colours.length).toBeGreaterThan(0)
    })

    it('opens a saved file listing folded to its file, with nothing unsaved', async () => {
      const listing = await seedFile()
      renderStorefront(`/storefront/listings/${listing.id}`)
      expect(await screen.findByTestId('listing-source')).toHaveTextContent('Desiccant_Box.stl')
      expect(screen.getByLabelText('Title')).toHaveValue('Desiccant box')
      expect(screen.queryByText('Unsaved changes')).toBeNull()
    })

    it('says a published file gone from the library still sells the published version', async () => {
      const listing = await seedFile()
      server.use(http.get('/api/v1/print/library/46', () => HttpResponse.json({ detail: 'gone' }, { status: 404 })))
      renderStorefront(`/storefront/listings/${listing.id}`)
      expect(await screen.findByText('This file is no longer in the library. Customers still get the version you published.')).toBeInTheDocument()
    })

    it('says a file changed in the library, and saves the request for the library’s version', async () => {
      const listing = await seedFile()
      const entry = await api.getLibraryFile(46)
      server.use(http.get('/api/v1/print/library/46', () => HttpResponse.json({ ...entry, file_hash: 'b'.repeat(64) })))
      const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
      expect(await screen.findByText('This file changed in the library since you published. Customers still get the version you published.')).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Use the library’s version' }))
      expect(screen.getByText('The next publish takes the library’s version.')).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Save draft' }))
      await waitFor(async () => expect((await stored())[0]?.library_file).toMatchObject({ id: 46, repin: true, pinned: listing.library_file?.pinned }))
    })

    it('asks before a pinned listing saves another file, and keeps it on Cancel', async () => {
      const listing = await seedFile()
      const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
      await user.click(within(await screen.findByTestId('listing-source')).getByRole('button', { name: 'Change' }))
      await user.click(within(screen.getByTestId('listing-file')).getByRole('button', { name: 'Change' }))
      await user.type(screen.getByLabelText('Library file'), 'drawer')
      await user.click(await screen.findByRole('button', { name: /^drawer-label\.3mf/ }))
      await user.click(screen.getByRole('button', { name: 'Save draft' }))
      const ask = await screen.findByRole('alertdialog')
      expect(ask).toHaveTextContent('Customers lose the version you published. The next publish sells the file you picked.')
      await user.click(within(ask).getByRole('button', { name: 'Cancel' }))
      expect((await stored())[0]?.library_file?.id).toBe(46)
      await user.click(screen.getByRole('button', { name: 'Save draft' }))
      await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Save' }))
      await waitFor(async () => expect((await stored())[0]?.library_file).toMatchObject({ id: 120, pinned: null }))
    })

    it('keeps the published version: a save with no request', async () => {
      const listing = await seedFile()
      await api.updateStorefrontListing(
        listing.id,
        { source: 'library_file', library_file: { id: 46, library: 'bambuddy', repin: true }, title: 'Desiccant box', colours: [FILAMENT_PLA] },
        listing.updated_at,
      )
      const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
      await user.click(await screen.findByRole('button', { name: 'Keep the published version' }))
      await waitFor(async () => expect((await stored())[0]?.library_file?.repin).toBe(false))
      expect(screen.queryByText('The next publish takes the library’s version.')).toBeNull()
    })

    it('saves a new listing before its first picture uploads, then adds the picture', async () => {
      const seen = recordRequests()
      const { user } = renderStorefront('/storefront/listings/new', <Where />)
      await user.click(await screen.findByRole('radio', { name: 'A library file' }))
      await user.type(screen.getByLabelText('Library file'), 'drawer')
      await user.click(await screen.findByRole('button', { name: /^drawer-label\.3mf/ }))
      await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('drawer-label'))
      const picture = new File([new Uint8Array([137, 80, 78, 71])], 'photo.png', { type: 'image/png' })
      await user.upload(screen.getByLabelText('Upload a picture'), picture)
      await waitFor(() => expect(within(screen.getByRole('group', { name: 'Pictures' })).getByText('Uploaded picture')).toBeInTheDocument())
      const [saved] = await stored()
      expect(seen.indexOf('POST /api/v1/storefront/listings')).toBeLessThan(seen.indexOf(`POST /api/v1/storefront/listings/${saved?.id}/pictures`))
      await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(`/storefront/listings/${saved?.id}`))
      await user.click(screen.getByRole('button', { name: 'Save draft' }))
      await waitFor(async () => expect((await stored())[0]?.media_ids).toEqual(['plate:1', expect.stringMatching(/^upload:/)]))
    })

    it('says what publish refused about the file under Source', async () => {
      const listing = await seedFile(46, false)
      server.use(
        http.post(`/api/v1/storefront/listings/${listing.id}/publish`, () =>
          HttpResponse.json(
            { type: 'https://scadbuddy.dev/problems/storefront/listing_file_too_large', title: 'Unprocessable Content', status: 422, detail: 'the file is larger than 512 MB, which a print cannot read', name: 'library_file' },
            { status: 422 },
          ),
        ),
      )
      const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
      await user.click(await screen.findByRole('button', { name: 'Publish' }))
      expect(await within(screen.getByRole('region', { name: 'Source' })).findByRole('alert')).toHaveTextContent('Not published: the file is larger than 512 MB, which a print cannot read.')
      // The page's own alert points there.
      expect(screen.getByText('Not published: the file needs a change. See the note under “What this listing sells” below.')).toBeInTheDocument()
    })

    it('opens a listing of the library file “Sell on storefront…” names', async () => {
      const { user } = renderStorefront('/storefront/listings/new?sell=library:120')
      await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('drawer-label'))
      expect(screen.getByRole('radio', { name: 'A library file' })).toBeChecked()
      expect(within(screen.getByTestId('listing-file')).getByText('drawer-label.3mf')).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Save draft' }))
      await waitFor(async () => expect((await stored())[0]?.library_file).toMatchObject({ id: 120, library: 'bambuddy' }))
    })
  })

  it('shows the shop’s text as text, never as HTML', async () => {
    const { user } = renderStorefront()
    const description = await screen.findByLabelText('Description')
    await user.type(description, '<img src=x onerror=alert(1)>')
    const preview = screen.getByTestId('listing-preview')
    expect(within(preview).getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
    expect(preview.querySelector('img[src="x"]')).toBeNull()
  })

  it('unpublishes a listing', async () => {
    const first = await seed('First')
    await api.publishStorefrontListing(first.id)
    const { user } = renderStorefront(`/storefront/listings/${first.id}`)
    await user.click(await screen.findByRole('button', { name: 'Unpublish' }))
    await waitFor(() => expect(screen.queryByText('Published')).toBeNull())
    expect((await stored()).every((l) => !l.published)).toBe(true)
  })

  it('offers every colour, from every priced filament, unless the person locks it (#6131)', async () => {
    const { user } = renderStorefront()
    expect(await screen.findByRole('switch', { name: 'Customer can change Plate' })).toBeChecked()
    expect(screen.getByRole('switch', { name: 'Customer can change Text' })).toBeChecked()
    // No filament is the keychain's colours, so every priced one is on offer, of any material.
    await user.click(within(screen.getByRole('group', { name: 'Colours on offer' })).getByRole('button', { name: /^Show all/ }))
    expect(screen.getByRole('checkbox', { name: 'PLA Sunflower Yellow' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'PETG Black' })).toBeChecked()
    // Locking is the person's choice.
    await user.click(screen.getByRole('switch', { name: 'Customer can change Text' }))
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(async () => expect(await stored()).toHaveLength(1))
    const [saved] = await stored()
    expect(saved?.colours).toEqual([FILAMENT_PLA, FILAMENT_PETG])
    // Stored as the backend stores it: an unset label is null. No filament has the template's
    // colour, so the colour starts at the first on offer (#6144).
    expect(saved?.params['body_color']).toEqual({ kind: 'choice', choices: ['#F9D72C', '#1E1E1E'], label: null, default: '#F9D72C' })
    expect(saved?.fixed['text_color']).toBe('#E8532F')
  })

  it('shows why a publish is refused', async () => {
    const detail = "'name-keychain' declares no shape for its inputs (model.json pipeline.inputs)"
    server.use(
      http.post('/api/v1/storefront/listings/:id/publish', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/listing_inputs_undeclared', title: 'Unprocessable Content', status: 422, detail },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderStorefront()
    await within(await screen.findByTestId('listing-preview')).findByLabelText('Name on the tag')
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    // #5701 — said in the Design block; the alert points there.
    expect(await within(await screen.findByRole('group', { name: 'Design' })).findByRole('alert')).toHaveTextContent(detail)
    expect(screen.getAllByRole('alert')[0]).toHaveTextContent('See the note under “Design” below.')
    expect(screen.queryByText('Published')).toBeNull()
  })

  it('names the parameter of a refusal while the design’s settings are not read (#7660 review)', async () => {
    server.use(
      http.get('/api/v1/models/:slug/versions/:commit/schema', () => HttpResponse.json({ detail: 'down' }, { status: 503 })),
      http.post('/api/v1/storefront/listings/:id/publish', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/listing_param_pipeline_set', title: 'Unprocessable Content', status: 422, detail: 'set by the pipeline', name: 'params.text_size' },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const listing = await seed('Open')
    const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('Open'))
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    // No Design block note exists without the schema, so the alert does not point there.
    expect(await screen.findByText('Not published: “text_size” needs a change. See the note under it below.')).toBeInTheDocument()
  })

  it('says a refusal of a parameter with no row in the alert itself (#7660 review)', async () => {
    server.use(
      http.post('/api/v1/storefront/listings/:id/publish', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/listing_default_invalid', title: 'Unprocessable Content', status: 422, detail: 'no', name: 'gone' },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderStorefront()
    await within(await screen.findByTestId('listing-preview')).findByLabelText('Name on the tag')
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    expect(await screen.findByText('Not published: the listing sets “gone”, a hidden parameter of this design that this editor does not show.')).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Design' })).toBeNull()
  })

  describe('a pipeline design (#5701)', () => {
    const HOUSE = { cols: 3, rows: 1, storeys: 2, windows: 4 }
    const seedHouse = (house: Record<string, number>, over: object = {}) =>
      api.createStorefrontListing({ slug: DOLLHOUSE_SLUG, commit: DOLLHOUSE_VERSION, title: 'House', inputs: { house }, inputs_v: 1, ...over })
    const design = () => screen.findByRole('group', { name: 'Design' })

    it('keeps its inputs through a save of another field, and publishes them', async () => {
      const listing = await seedHouse(HOUSE)
      const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
      expect(await design()).toHaveTextContent('House: Modules wide 3, Modules deep 1, Storeys 2, Windows per storey 4')
      expect(screen.queryByText('Unsaved changes')).toBeNull()
      await user.type(screen.getByLabelText('Title'), '!')
      await user.click(screen.getByRole('button', { name: 'Publish' }))
      expect(await screen.findByText('Published')).toBeInTheDocument()
      const [saved] = await stored()
      expect(saved).toMatchObject({ title: 'House!', inputs: { house: HOUSE }, inputs_v: 1, snapshot: { pipeline: true, inputs: { house: HOUSE } } })
    })

    it('names what publish refused in the Design block, until the next edit', async () => {
      const listing = await seedHouse({ ...HOUSE, storeys: 9 })
      const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
      const block = await design()
      await user.click(screen.getByRole('button', { name: 'Publish' }))
      expect(await within(block).findByRole('alert')).toHaveTextContent('Storeys: at most 3.')
      expect(screen.getAllByRole('alert')[0]).toHaveTextContent('See the note under “Design” below.')
      await user.type(screen.getByLabelText('Title'), '!')
      expect(within(block).queryByRole('alert')).toBeNull()
    })

    it('has no settings of the parameters its pipeline sets, and takes them off the listing', async () => {
      const listing = await seedHouse(HOUSE, { params: { piece: { kind: 'choice', choices: ['wall'] } } })
      const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
      await design()
      const settings = screen.getByRole('region', { name: 'Settings' })
      expect(await within(settings).findByRole('group', { name: 'Course height' })).toBeInTheDocument()
      expect(within(settings).queryByRole('group', { name: 'Piece' })).toBeNull()
      await user.click(screen.getByRole('button', { name: 'Save draft' }))
      await waitFor(async () => expect((await stored())[0]?.params).toEqual({}))
    })

    it('drops them with the template, and keeps them at another version of it', async () => {
      const listing = await seedHouse(HOUSE)
      const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
      await design()
      await user.click(screen.getByRole('button', { name: 'Change' }))
      await user.click(screen.getByRole('combobox', { name: 'Version' }))
      await user.click(screen.getAllByRole('option')[1]!)
      expect(await design()).toHaveTextContent('Modules wide 3')
      await user.click(screen.getByRole('combobox', { name: 'Design' }))
      await user.click(screen.getByRole('option', { name: 'Name Keychain' }))
      await within(await screen.findByTestId('listing-preview')).findByLabelText('Name on the tag')
      expect(screen.queryByRole('group', { name: 'Design' })).toBeNull()
    })
  })

  it('says beside the materials that publish refused a colour of a material the listing does not allow (#6258)', async () => {
    const { user } = renderStorefront()
    await within(await screen.findByTestId('listing-preview')).findByLabelText('Name on the tag')
    const allowed = screen.getByRole('group', { name: 'Materials allowed' })
    // The shop's default until the listing chooses its own.
    expect(await within(allowed).findByText('Shop default (PETG, PLA)')).toBeInTheDocument()
    await user.click(within(allowed).getByRole('button', { name: 'Choose for this listing' }))
    await user.click(within(allowed).getByRole('button', { name: 'Remove PETG' }))
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    expect(await within(allowed).findByRole('alert')).toHaveTextContent('Not published: PETG Black is PETG, which this listing does not allow.')
    expect(screen.getAllByRole('alert')[0]).toHaveTextContent('See “Materials allowed” below.')
    expect(screen.queryByText('Published')).toBeNull()
    // The edit that fixes it clears the message; the next publish goes through.
    await user.click(within(screen.getByTestId('materials-not-allowed')).getByRole('button', { name: 'Allow PETG' }))
    expect(within(allowed).queryByRole('alert')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    expect(await screen.findByText('Published')).toBeInTheDocument()
    const [saved] = await stored()
    expect(saved?.materials).toEqual(['PLA', 'PETG'])
  })

  it('says beside the colours that publish found no material to print the locked colours in (#6258)', async () => {
    server.use(
      http.post('/api/v1/storefront/listings/:id/publish', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/listing_locked_colours_unprintable', title: 'Unprocessable Content', status: 422, detail: 'no allowed material offers every locked colour', names: ['text_color'] },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderStorefront()
    await within(await screen.findByTestId('listing-preview')).findByLabelText('Name on the tag')
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    const colours = screen.getByRole('group', { name: 'Colours on offer' })
    expect(await within(colours).findByRole('alert')).toHaveTextContent('Not published: no allowed material has the locked colour of Text')
  })

  it('says why an edit of a listing changed elsewhere is refused', async () => {
    const listing = await seed('Mine')
    const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
    const title = await screen.findByLabelText('Title')
    await api.updateStorefrontListing(listing.id, { slug: KEYCHAIN, commit: COMMIT, title: 'Theirs' }, listing.updated_at)
    await user.type(title, '!')
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('changed since it was read')
  })

  it('keeps the listing opened while a publish of another was running', async () => {
    const first = await seed('First')
    const second = await seed('Second')
    const release = hold('post', '/api/v1/storefront/listings/:id/publish')
    const { user } = renderStorefront(`/storefront/listings/${first.id}`, <GoTo to={`/storefront/listings/${second.id}`} />)
    await screen.findByLabelText('Title')
    await user.click(await screen.findByRole('button', { name: 'Publish' }))
    await user.click(screen.getByRole('button', { name: 'Go' }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('Second'))
    release()
    await waitFor(async () => expect((await stored()).find((l) => l.title === 'First')?.published).toBe(true))
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(screen.getByLabelText('Title')).toHaveValue('Second')
    expect(screen.queryByText('Published')).toBeNull()
  })

  it('stays on the listing opened while a delete of another was running', async () => {
    const first = await seed('First')
    const second = await seed('Second')
    const release = hold('delete', '/api/v1/storefront/listings/:id')
    const { user } = renderStorefront(`/storefront/listings/${first.id}`, <GoTo to={`/storefront/listings/${second.id}`} />)
    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Go' }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('Second'))
    release()
    await waitFor(async () => expect(await stored()).toHaveLength(1))
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(screen.getByLabelText('Title')).toHaveValue('Second')
  })

  it('goes back to the list after a delete', async () => {
    const listing = await seed('Gone soon')
    const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    expect(await screen.findByText(/No listings yet/)).toBeInTheDocument()
  })

  it('says to choose a filament for a colour parameter left with none', async () => {
    const seen = recordRequests()
    const { user } = renderStorefront()
    await screen.findByRole('switch', { name: 'Customer can change Plate' })
    await user.click(within(screen.getByRole('group', { name: 'Colours on offer' })).getByRole('button', { name: /^Show all/ }))
    await user.click(await screen.findByRole('checkbox', { name: 'PLA Sunflower Yellow' }))
    await user.click(screen.getByRole('checkbox', { name: 'PETG Black' }))
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/“Plate” is offered with nothing to choose.*tick a colour on offer/)
    expect(seen.filter((r) => r.startsWith('POST /api/v1/storefront/listings'))).toEqual([])
  })

  it('publishes again, unchanged, without saving first', async () => {
    const seen = recordRequests()
    const { user } = renderStorefront()
    await within(await screen.findByTestId('listing-preview')).findByLabelText('Name on the tag')
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    expect(await screen.findByText('Published')).toBeInTheDocument()
    const [listing] = await stored()
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    await waitFor(() => expect(seen.filter((r) => r === `POST /api/v1/storefront/listings/${listing?.id}/publish`)).toHaveLength(2))
    expect(seen.filter((r) => r.startsWith('PUT /api/v1/storefront/listings/'))).toEqual([])
  })

  it('publishes an unchanged published listing again without taking it down first', async () => {
    const listing = await seed('Live')
    await api.publishStorefrontListing(listing.id)
    const seen = recordRequests()
    const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
    await within(await screen.findByTestId('listing-preview')).findByText('Live')
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    await waitFor(() => expect(seen).toContain(`POST /api/v1/storefront/listings/${listing.id}/publish`))
    expect(seen.filter((r) => r.startsWith('PUT /api/v1/storefront/listings/'))).toEqual([])
  })

  it('drops the parameters a newly chosen version does not have', async () => {
    const older = versionIds.edited
    server.use(
      http.get('/api/v1/models/:slug/versions/:commit/schema', ({ params }) =>
        params['commit'] === older
          ? HttpResponse.json({ ...keychainSchema, parameters: (keychainSchema.parameters ?? []).filter((p) => p.name !== 'text_size' && p.name !== 'padding') })
          : undefined,
      ),
    )
    const listing = await api.createStorefrontListing({
      slug: KEYCHAIN,
      commit: versionIds.raised,
      title: 'Tag',
      params: { text_size: { kind: 'number', min: 6, max: 20, step: null } },
      fixed: { padding: 6, corner_radius: 4 },
    })
    const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
    await user.click(await screen.findByRole('button', { name: 'Change' }))
    await user.click(await screen.findByRole('combobox', { name: 'Version' }))
    await user.click(await screen.findByRole('option', { name: /Update name-keychain metadata/ }))
    await waitFor(() => expect(screen.queryByRole('group', { name: /Text size/ })).toBeNull())
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(async () => expect((await stored())[0]?.commit).toBe(older))
    const [saved] = await stored()
    expect(saved?.params).toEqual({})
    expect(saved?.fixed).toEqual({ corner_radius: 4 })
  })

  it('keeps unsaved edits when a link to another listing arrives, and says so (#6140 review)', async () => {
    const listing = await seed('Linked')
    const { user } = renderStorefront('/storefront/listings/new', <GoTo to={`/storefront/listings/${listing.id}`} />)
    const title = await screen.findByLabelText('Title')
    // The design's name fills the untouched title first; clearing an empty field is no
    // edit, so without this wait the name could land between the clear and the typing.
    await waitFor(() => expect(title).toHaveValue('Name Keychain'))
    await user.clear(title)
    await user.type(title, 'Mine')
    await user.click(screen.getByRole('button', { name: 'Go' }))
    // The link is held, and the page asks (#6157 review): Stay keeps the edits.
    const ask = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    await user.click(within(ask).getByRole('button', { name: 'Stay' }))
    expect(screen.getByLabelText('Title')).toHaveValue('Mine')
    // Leave opens the link.
    await user.click(screen.getByRole('button', { name: 'Go' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Leave without saving' }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('Linked'))
  })

  it('shows the design picker on a new listing, and another design can be chosen', async () => {
    const { user } = renderStorefront()
    const design = await screen.findByRole('combobox', { name: 'Design' })
    await waitFor(() => expect(design).toHaveTextContent('Name Keychain'))
    expect(screen.queryByRole('button', { name: 'Change' })).toBeNull()
    await user.click(design)
    await user.click(await screen.findByRole('option', { name: /Coaster/ }))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Design' })).toHaveTextContent(/Coaster/))
    expect(screen.getByRole('combobox', { name: 'Version' })).toBeInTheDocument()
  })

  it('folds the design picker to one line once a new listing is saved (#6711 review)', async () => {
    const { user } = renderStorefront()
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Design' })).toHaveTextContent('Name Keychain'))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Customer can change Plate' })).toBeChecked())
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByTestId('listing-design')).toHaveTextContent('Name Keychain')
    expect(screen.getByRole('button', { name: 'Change' })).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Design' })).toBeNull()
  })

  it('keeps a title typed before the templates answer (#6290)', async () => {
    const release = hold('get', '/api/v1/models')
    const { user } = renderStorefront()
    const title = await screen.findByLabelText('Title')
    await user.type(title, 'Mine')
    release()
    // The first template is chosen once the list answers; the typed title stays.
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Design' })).toHaveTextContent('Name Keychain'))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Customer can change Plate' })).toBeChecked())
    expect(screen.getByLabelText('Title')).toHaveValue('Mine')
  })

  it('keeps a title cleared before the templates answer (#6290)', async () => {
    const release = hold('get', '/api/v1/models')
    const { user } = renderStorefront()
    const title = await screen.findByLabelText('Title')
    await user.type(title, 'x')
    await user.clear(title)
    release()
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Design' })).toHaveTextContent('Name Keychain'))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Customer can change Plate' })).toBeChecked())
    expect(screen.getByLabelText('Title')).toHaveValue('')
  })

  it('folds the colours on offer once the smart defaults tick some (#6292 review)', async () => {
    renderStorefront()
    // A new listing mounts with none ticked, so the list is open until the defaults land.
    expect(await screen.findByRole('switch', { name: 'Customer can change Plate' })).toBeChecked()
    const colours = screen.getByRole('group', { name: 'Colours on offer' })
    await waitFor(() => expect(within(colours).queryByRole('list')).toBeNull())
    expect(colours).toHaveTextContent(/\d+ ticked \(/)
  })

  it('folds the colours on offer of a saved listing when the price book answers late (#6292 review)', async () => {
    const listing = await api.createStorefrontListing({ slug: KEYCHAIN, commit: COMMIT, title: 'Tag', colours: [FILAMENT_PLA] })
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.get('/api/v1/orders/price-book', async () => {
        await gate
        return undefined
      }),
    )
    renderStorefront(`/storefront/listings/${listing.id}`)
    expect(await screen.findByLabelText('Title')).toHaveValue('Tag')
    release()
    const colours = screen.getByRole('group', { name: 'Colours on offer' })
    await waitFor(() => expect(colours).toHaveTextContent(/1 ticked \(PLA /))
    expect(within(colours).queryByRole('list')).toBeNull()
  })

  describe('says what the price book holds once it has answered, whatever the defaults do (#6292 review)', () => {
    const PRICE_BOOK = '/api/v1/orders/price-book'
    const SCHEMA = '/api/v1/models/:slug/versions/:commit/schema'
    async function emptyBook() {
      const book = await api.priceBook()
      server.use(http.get(PRICE_BOOK, () => HttpResponse.json({ ...book, filaments: [] })))
    }
    const colours = () => screen.getByRole('group', { name: 'Colours on offer' })

    it('when the design’s settings fail', async () => {
      await emptyBook()
      server.use(http.get(SCHEMA, () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
      renderStorefront()
      expect(await screen.findByText(/^The design’s settings could not be read\./)).toBeInTheDocument()
      await waitFor(() => expect(colours()).toHaveTextContent('The price book has no filaments yet.'))
      expect(within(colours()).getByRole('link', { name: 'Add one' })).toBeInTheDocument()
      expect(screen.queryByText('Loading the design’s settings…')).toBeNull()
    })

    it('when there is no design to choose', async () => {
      await emptyBook()
      server.use(http.get('/api/v1/models', () => HttpResponse.json([])))
      renderStorefront()
      await waitFor(() => expect(colours()).toHaveTextContent('The price book has no filaments yet.'))
    })

    it('while a design change waits for its settings', async () => {
      await emptyBook()
      const { user } = renderStorefront()
      await waitFor(() => expect(screen.getByRole('combobox', { name: 'Design' })).toHaveTextContent('Name Keychain'))
      await waitFor(() => expect(screen.queryByText('Loading the design’s settings…')).toBeNull())
      release = hold('get', SCHEMA)
      await user.click(screen.getByRole('combobox', { name: 'Design' }))
      await user.click(await screen.findByRole('option', { name: /Coaster/ }))
      // The new design's defaults wait on its settings, which are held.
      expect(screen.getByRole('combobox', { name: 'Design' })).toHaveTextContent(/Coaster/)
      expect(colours()).toHaveTextContent('The price book has no filaments yet.')
      expect(colours()).not.toHaveTextContent('Reading the price book')
    })

    it('when the price book fails', async () => {
      server.use(http.get(PRICE_BOOK, () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
      renderStorefront()
      await waitFor(() => expect(colours()).toHaveTextContent('The price book could not be read'))
      expect(colours()).not.toHaveTextContent('Reading the price book')
    })

    let release: () => void = () => undefined
    afterEach(() => release())
  })

  it('keeps the ticked filaments across a design change (#6292 review)', async () => {
    const { user } = renderStorefront()
    expect(await screen.findByRole('switch', { name: 'Customer can change Plate' })).toBeChecked()
    const group = () => screen.getByRole('group', { name: 'Colours on offer' })
    await user.click(within(group()).getByRole('button', { name: /^Show all/ }))
    // Only PLA Sunflower Yellow ticked, by hand.
    await user.click(within(group()).getByRole('checkbox', { name: 'PETG Black' }))
    await user.click(screen.getByRole('combobox', { name: 'Design' }))
    await user.click(await screen.findByRole('option', { name: /Coaster/ }))
    await waitFor(() => expect(screen.queryByText('Loading the design’s settings…')).toBeNull())
    expect(within(group()).getByRole('checkbox', { name: 'PLA Sunflower Yellow' })).toBeChecked()
    expect(within(group()).getByRole('checkbox', { name: 'PETG Black' })).not.toBeChecked()
  })

  it('says nothing is unsaved when a saved listing opens (#6290)', async () => {
    // Stored with an offered colour, an offered number and an offered text: each bound's
    // unset label and default come back null, as the backend answers them.
    const listing = await api.createStorefrontListing({
      slug: KEYCHAIN,
      commit: COMMIT,
      title: 'Tag',
      colours: [FILAMENT_PLA, FILAMENT_PETG],
      params: { body_color: { kind: 'choice', choices: ['#F9D72C', '#1E1E1E'] }, text_size: { kind: 'number', min: 6, max: 28 }, name: { kind: 'text', max_length: 20 } },
      fixed: { text_color: '#E8532F' },
    })
    expect((await api.storefrontListing(listing.id)).params['text_size']).toMatchObject({ label: null, default: null, step: null })
    renderStorefront(`/storefront/listings/${listing.id}`)
    expect(await screen.findByLabelText('Title')).toHaveValue('Tag')
    expect(await screen.findByRole('switch', { name: 'Customer can change Plate' })).toBeChecked()
    await waitFor(() => expect(screen.queryByText('Loading the design’s settings…')).toBeNull())
    expect(screen.queryByText('Unsaved changes')).toBeNull()
  })

  it('keeps an edited saved listing when its design’s settings fail to load (#6140 review)', async () => {
    server.use(http.get('/api/v1/models/:slug/versions/:commit/schema', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
    const open = await seed('Open')
    const other = await seed('Other')
    const { user } = renderStorefront(`/storefront/listings/${open.id}`, <GoTo to={`/storefront/listings/${other.id}`} />)
    const title = await screen.findByLabelText('Title')
    await waitFor(() => expect(title).toHaveValue('Open'))
    await user.type(title, ' edited')
    await user.click(screen.getByRole('button', { name: 'Go' }))
    expect(await screen.findByRole('alertdialog', { name: 'Leave without saving?' })).toBeInTheDocument()
    expect(screen.getByLabelText('Title')).toHaveValue('Open edited')
  })

  it('asks before Back, or any other link, drops unsaved edits (#6157 review)', async () => {
    const listing = await seed('Edited')
    const { user } = renderStorefront(`/storefront/listings/${listing.id}`, <a href="/models">Models</a>)
    await user.type(await screen.findByLabelText('Title'), '!')
    await user.click(screen.getByRole('link', { name: 'Back to listings' }))
    await user.click(within(await screen.findByRole('alertdialog', { name: 'Leave without saving?' })).getByRole('button', { name: 'Stay' }))
    expect(screen.getByLabelText('Title')).toHaveValue('Edited!')
    // A link outside the editor, as the sidebar's, is held too.
    await user.click(screen.getByRole('link', { name: 'Models' }))
    expect(await screen.findByRole('alertdialog', { name: 'Leave without saving?' })).toBeInTheDocument()
    expect(screen.getByLabelText('Title')).toHaveValue('Edited!')
  })

  it('warns that saving a published listing takes it off the store', async () => {
    const listing = await seed('Live')
    await api.publishStorefrontListing(listing.id)
    const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
    const title = await screen.findByLabelText('Title')
    expect(screen.queryByText(/takes this listing off the store/)).toBeNull()
    await user.type(title, '!')
    expect(screen.getByText(/takes this listing off the store/)).toBeInTheDocument()
  })

  it('says why Publish waits for a title', async () => {
    const { user } = renderStorefront()
    await user.clear(await screen.findByLabelText('Title'))
    expect(screen.getByText('Give the product a title to publish.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Publish' })).toBeDisabled()
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

    it('opens the listing filled in from a render, unsaved, each colour starting at one on offer, and publishes it', async () => {
      serveJob()
      const seen = recordRequests()
      // The old address still works: the list sends it on to the new listing.
      const { user } = renderStorefront('/storefront?sell=job:j1')
      // The slow render is said before publishing (#6120); the note shows once the fill-in is in.
      expect(await screen.findByRole('note')).toHaveTextContent('took 150 s to render')
      expect(screen.getByLabelText('Title')).toHaveValue('Name Keychain')
      // The design and the version the render read.
      expect(screen.getByRole('combobox', { name: 'Design' })).toHaveTextContent('Name Keychain')
      await waitFor(() => expect(screen.getByRole('combobox', { name: 'Version' })).toHaveTextContent(versionIds.edited.slice(0, 7)))
      // Both colours are offered; the plate's yellow is a PLA, so the PLA filaments are on offer.
      // No filament on offer has either template default, so each starts at one that is (#6144):
      // the plate at the yellow the render used, the text at the first on offer.
      await user.click(within(screen.getByRole('group', { name: 'Colours on offer' })).getByRole('button', { name: /^Show all/ }))
      expect(screen.getByRole('checkbox', { name: 'PLA Sunflower Yellow' })).toBeChecked()
      expect(screen.getByRole('checkbox', { name: 'PETG Black' })).not.toBeChecked()
      expect(screen.queryByTestId('colour-problems')).not.toBeInTheDocument()
      expect(seen.filter((r) => r.startsWith('POST /api/v1/storefront/listings'))).toEqual([])

      await user.click(screen.getByRole('button', { name: 'Publish' }))
      expect(await screen.findByText('Published')).toBeInTheDocument()

      const [saved] = await stored()
      expect(saved?.commit).toBe(versionIds.edited)
      expect(saved?.params).toEqual({
        name: { kind: 'text', max_length: 20, label: null, default: null },
        body_color: { kind: 'choice', choices: ['#F9D72C'], label: null, default: '#f9d72c' },
        text_color: { kind: 'choice', choices: ['#F9D72C'], label: null, default: '#F9D72C' },
      })
      expect(saved?.fixed).toMatchObject({ text_size: 20, thickness: 5.2 })
      expect(saved?.colours).toEqual([FILAMENT_PLA])
      expect(saved?.media_ids).toEqual(['thumbnail'])
    })

    it('takes the output’s name, less a colour the customer chooses, and the current version for one outside the history', async () => {
      serveJob({ model_version: 'f'.repeat(40) })
      server.use(
        // A plate whose default is the price book's yellow: offered, so its name leaves the title.
        http.get('/api/v1/models/:slug/versions/:commit/schema', () =>
          HttpResponse.json({ ...keychainSchema, parameters: (keychainSchema.parameters ?? []).map((p) => (p.name === 'body_color' ? { ...p, initial: '#F9D72C' } : p)) }),
        ),
        http.get('/api/v1/outputs/:id', ({ params }) =>
          HttpResponse.json({
            id: params['id'],
            slug: KEYCHAIN,
            model_version: 'f'.repeat(40),
            name: 'Name tag – Sunflower Yellow',
            job_id: 'j2',
            created_at: '2026-11-13T10:00:00Z',
            bbox_mm: { x: 1, y: 1, z: 1 },
            params: { name: 'Mum' },
            has_thumbnail: false,
          }),
        ),
      )
      renderStorefront('/storefront/listings/new?sell=output:o1')
      // The blank new listing shows first; the fill-in replaces it.
      await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('Name tag'))
      await waitFor(() => expect(screen.getByRole('combobox', { name: 'Version' })).toHaveTextContent(`${versionIds.raised.slice(0, 7)}`))
      expect(screen.getByRole('combobox', { name: 'Version' })).toHaveTextContent('(current)')
      expect(screen.getAllByRole('note')[0]).toHaveTextContent('not in the design')
    })

    it('takes a pipeline template’s own inputs and their version, which publish brings up to date (#5701)', async () => {
      serveJob({ slug: DOLLHOUSE_SLUG, model_version: DOLLHOUSE_VERSION, params: { piece: 'roof_panel', course_height: 220 }, inputs: { params: { piece: 'roof_panel', course_height: 220 }, v: 0 } })
      const { user } = renderStorefront('/storefront/listings/new?sell=job:j1')
      expect(await screen.findByText('The design the template starts with')).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Publish' }))
      expect(await screen.findByText('Published')).toBeInTheDocument()
      const [saved] = await stored()
      expect(saved).toMatchObject({ inputs: {}, inputs_v: 0, snapshot: { inputs: { house: { cols: 2 } }, inputs_version: 1 } })
      expect(saved?.fixed).toMatchObject({ course_height: 220 })
      expect({ ...saved?.fixed, ...saved?.params }).not.toHaveProperty('piece')
    })

    it('leaves a listing opened while the fill-in ran alone when the fill-in answers (#6140 review)', async () => {
      const mine = await seed('Mine')
      serveJob()
      let release: () => void = () => undefined
      const gate = new Promise<void>((resolve) => {
        release = resolve
      })
      server.use(
        http.get('/api/v1/models/:slug/versions', async () => {
          await gate
          return undefined
        }),
      )
      const { user } = renderStorefront('/storefront/listings/new?sell=job:j1', <GoTo to={`/storefront/listings/${mine.id}`} />)
      await screen.findByText('Filling in the listing…')
      await user.click(screen.getByRole('button', { name: 'Go' }))
      await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('Mine'))
      await user.type(screen.getByLabelText('Title'), '!')
      release()
      await waitFor(() => expect(screen.queryByText('Filling in the listing…')).toBeNull())
      expect(screen.getByLabelText('Title')).toHaveValue('Mine!')
      // The person is told why their Sell click did nothing.
      expect(screen.getByRole('alert')).toHaveTextContent('was not opened, because another listing was opened meanwhile')
    })

    it('fills the listing in when the models list fails to load, named by its slug (#6140 review)', async () => {
      serveJob()
      server.use(http.get('/api/v1/models', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
      renderStorefront('/storefront/listings/new?sell=job:j1')
      await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue(KEYCHAIN))
    })

    it('says why a sell address that names nothing opens nothing', async () => {
      renderStorefront('/storefront/listings/new?sell=nonsense')
      expect(await screen.findByRole('alert')).toHaveTextContent('Could not fill in the listing: Nothing to sell: "nonsense" names no render or saved output.')
    })

    it('says why a listing could not be filled in', async () => {
      server.use(
        http.get('/api/v1/jobs/:id', () =>
          HttpResponse.json({ title: 'Not Found', status: 404, detail: 'no such job' }, { status: 404, headers: { 'Content-Type': 'application/problem+json' } }),
        ),
      )
      renderStorefront('/storefront/listings/new?sell=job:gone')
      expect(await screen.findByRole('alert')).toHaveTextContent('Could not fill in the listing: no such job')
    })

    it('opens the listing the assistant names', async () => {
      const listing = await seed('From the assistant')
      renderStorefront(`/storefront?listing=${listing.id}`)
      expect(await screen.findByLabelText('Title')).toHaveValue('From the assistant')
    })
  })

  it('lists every picture the listing holds, so what it shows is what Save draft sends (#6213)', async () => {
    const keychain = (await api.getModel(KEYCHAIN)).media ?? []
    const video: MediaView = { id: 'd4e5f6a1b2c3', file: 'd4e5f6a1b2c3.mp4', kind: 'video', caption: 'Clip', poster: null, missing: false, readonly: false, content_type: 'video/mp4', size: 24 }
    setMockMedia(KEYCHAIN, [...keychain, video])
    const listing = await api.createStorefrontListing({ slug: KEYCHAIN, commit: COMMIT, title: 'Tag', media_ids: [video.id, 'thumbnail', '0123456789ab'] })
    const { user } = renderStorefront(`/storefront/listings/${listing.id}`)

    await waitFor(() => expect(screen.getAllByTestId('listing-picture').map((row) => row.textContent)).toEqual([
      expect.stringContaining('A video'),
      expect.stringContaining('thumbnail.png'),
      expect.stringContaining('Not in the design’s media'),
    ]))
    await user.click(screen.getByRole('button', { name: 'Remove Clip' }))
    await user.click(screen.getByRole('button', { name: 'Remove 0123456789ab' }))
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(async () => expect((await api.storefrontListing(listing.id)).media_ids).toEqual(['thumbnail']))
    expect(screen.getAllByTestId('listing-picture')).toHaveLength(1)
  })

  it('keeps the edits made while a picture uploads, and adds the picture to them (#6213)', async () => {
    const listing = await api.createStorefrontListing({ slug: KEYCHAIN, commit: COMMIT, title: 'Tag', media_ids: ['thumbnail'] })
    let finish: () => void = () => undefined
    const gate = new Promise<void>((resolve) => (finish = resolve))
    vi.spyOn(api, 'uploadMedia').mockImplementation(async (slug) => {
      await gate
      const model = await api.getModel(slug)
      const added: MediaView = { id: 'f6a1b2c3d4e5', file: 'f6a1b2c3d4e5.png', kind: 'image', caption: '', poster: null, missing: false, readonly: false, content_type: 'image/png', size: 8 }
      return { ...model, media: [...(model.media ?? []), added] }
    })
    const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
    const title = await screen.findByLabelText('Title')
    await user.upload(screen.getByLabelText('Upload a picture'), new File(['png'], 'new.png', { type: 'image/png' }))
    expect(await screen.findByText(/Uploading new\.png/)).toBeInTheDocument()

    // While it uploads: the title changes and the old picture is taken out.
    await user.clear(title)
    await user.type(title, 'Name tag')
    await user.click(screen.getByRole('button', { name: /^Remove / }))
    finish()

    await waitFor(() => expect(screen.getAllByTestId('listing-picture')).toHaveLength(1))
    expect(screen.getByLabelText('Title')).toHaveValue('Name tag')
    expect(within(screen.getByTestId('listing-picture')).getByRole('img')).toHaveAttribute('src', expect.stringContaining('f6a1b2c3d4e5'))
  })

  it('marks the pictures the public site shows as live (#6213)', async () => {
    const listing = await api.createStorefrontListing({ slug: KEYCHAIN, commit: COMMIT, title: 'Tag', media_ids: ['thumbnail'] })
    await api.publishStorefrontListing(listing.id)
    const { user } = renderStorefront(`/storefront/listings/${listing.id}`)
    const [row] = await screen.findAllByTestId('listing-picture')
    expect(within(row!).getByText('Live')).toBeInTheDocument()
    // Taken out of the draft, it is offered to add back.
    await user.click(within(row!).getByRole('button', { name: /^Remove / }))
    expect(screen.queryByTestId('listing-picture')).toBeNull()
    expect(screen.getByRole('button', { name: /^Add / })).toBeInTheDocument()
  })
})
