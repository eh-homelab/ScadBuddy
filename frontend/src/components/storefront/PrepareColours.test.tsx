import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import type { StorefrontListing } from '../../api/types'
import { failNextPrepareStarts, setPrepareCap } from '../../mocks/features/storefront'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { PrepareColours } from './PrepareColours'

const COMMIT = 'c3d4e5f6a7b8'.repeat(4).slice(0, 40)
const COLOURS = { kind: 'choice' as const, choices: ['#FF0000', '#ff0000', '#0000FF', '#00FF00'] }

async function published(params: StorefrontListing['params']): Promise<StorefrontListing> {
  const made = await api.createStorefrontListing({ slug: 'name-keychain', commit: COMMIT, title: 'Tag', params })
  return api.publishStorefrontListing(made.id)
}

function show(listing: StorefrontListing) {
  return renderPage(<PrepareColours listingId={listing.id} version={listing.updated_at} pollMs={20} />)
}

describe('PrepareColours (#6237)', () => {
  beforeEach(() => resetMockState())

  it('shows the count, asks to confirm, then shows the renders getting ready', async () => {
    const listing = await published({ body: COLOURS, trim: { kind: 'choice', choices: ['#FF0000', '#0000FF'] } })
    const { user } = show(listing)
    // Three colours (red twice in two cases is one) by two.
    expect(await screen.findByText('6 colour combinations')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Prepare every colour combination' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Prepare 6 colour combinations?' })
    // #6299 review: a few at a time, at the normal priority, never behind other renders.
    expect(dialog).toHaveTextContent("ScadBuddy renders a few at a time, so your own renders and your customers' are not held up.")
    await user.click(screen.getByRole('button', { name: 'Prepare' }))
    expect(await screen.findByRole('progressbar', { name: 'Colour combinations ready' })).toBeInTheDocument()
    // Read again while renders are in the queue, until every one is ready.
    await waitFor(() => expect(screen.getByText('(6 of 6 ready)')).toBeInTheDocument())
    expect(screen.getByRole('progressbar', { name: 'Colour combinations ready' })).toHaveAttribute('aria-valuenow', '6')
    expect(screen.queryByRole('button', { name: /Prepare/ })).not.toBeInTheDocument()
  })

  it('keeps reading after one read fails during a run, and clears its error (#6299 review)', async () => {
    const listing = await published({ body: COLOURS, trim: { kind: 'choice', choices: ['#FF0000', '#0000FF'] } })
    const { user } = show(listing)
    await user.click(await screen.findByRole('button', { name: 'Prepare every colour combination' }))
    await user.click(await screen.findByRole('button', { name: 'Prepare' }))
    await screen.findByRole('progressbar', { name: 'Colour combinations ready' })
    // One read during the run fails; the next ones answer again.
    server.use(
      http.get(
        `/api/v1/storefront/listings/${listing.id}/prepare`,
        () => HttpResponse.json({ type: 'render_unavailable', detail: 'The shop could not read these renders.' }, { status: 503 }),
        { once: true },
      ),
    )
    expect(await screen.findByRole('alert')).toHaveTextContent('The shop could not read these renders.')
    await waitFor(() => expect(screen.getByText('(6 of 6 ready)')).toBeInTheDocument())
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('names the renders that could not be started, and offers to prepare again (#6299 review)', async () => {
    failNextPrepareStarts(1)
    const listing = await published({ body: COLOURS })
    const { user } = show(listing)
    await user.click(await screen.findByRole('button', { name: 'Prepare every colour combination' }))
    await user.click(await screen.findByRole('button', { name: 'Prepare' }))
    expect(await screen.findByText('The render of 1 combination could not be started. Prepare again to try them again.')).toBeInTheDocument()
    expect(screen.getByText('(2 of 3 ready)')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Prepare again' })).toBeInTheDocument()
  })

  it('says the count and the cap, and offers no start, over the cap', async () => {
    setPrepareCap(4)
    const listing = await published({ body: COLOURS, trim: COLOURS })
    show(listing)
    expect(await screen.findByText('This listing has 9 colour combinations. The shop prepares at most 4 at a time. Offer fewer colours to prepare them.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Prepare/ })).not.toBeInTheDocument()
  })

  it('shows nothing for a listing with no colour to choose', async () => {
    const listing = await published({ width: { kind: 'number', min: 20, max: 80, step: 1 } })
    const { container } = show(listing)
    await waitFor(() => expect(container).toBeEmptyDOMElement())
    expect(screen.queryByText(/colour combination/)).not.toBeInTheDocument()
  })
})
