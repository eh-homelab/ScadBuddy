import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { resetMockState } from '../mocks/handlers'
import { FILAMENT_PLA } from '../mocks/features/orders/state'
import { renderPage } from '../test/utils'
import { StorefrontPage } from './StorefrontPage'

const KEYCHAIN = 'name-keychain'
const COMMIT = 'c3d4e5f6a7b8'.repeat(4).slice(0, 40)

async function seed(title: string) {
  return api.createStorefrontListing({ slug: KEYCHAIN, commit: COMMIT, title })
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
})
