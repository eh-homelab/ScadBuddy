import { screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import type { StorefrontListing } from '../../api/types'
import { setPrepareCap } from '../../mocks/features/storefront'
import { resetMockState } from '../../mocks/handlers'
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
    expect(await screen.findByRole('alertdialog', { name: 'Prepare 6 colour combinations?' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Prepare' }))
    expect(await screen.findByRole('progressbar', { name: 'Colour combinations ready' })).toBeInTheDocument()
    // Read again while renders are in the queue, until every one is ready.
    await waitFor(() => expect(screen.getByText('(6 of 6 ready)')).toBeInTheDocument())
    expect(screen.getByRole('progressbar', { name: 'Colour combinations ready' })).toHaveAttribute('aria-valuenow', '6')
    expect(screen.queryByRole('button', { name: /Prepare/ })).not.toBeInTheDocument()
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
