import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import type { MediaView, StorefrontListingInput } from '../../api/types'
import { GALLERY_SLUG } from '../../mocks/fixtures'
import { refuseNextRepublish } from '../../mocks/features/storefront'
import { renderPage } from '../../test/utils'
import { StorefrontPicture } from './StorefrontPicture'

const COMMIT = 'a'.repeat(40)

function body(title: string, media_ids: string[] = []): StorefrontListingInput {
  return { slug: GALLERY_SLUG, commit: COMMIT, title, description: '', params: {}, fixed: {}, colours: [], media_ids }
}

async function image(): Promise<MediaView> {
  const found = ((await api.getModel(GALLERY_SLUG)).media ?? []).find((item) => item.id === 'c3d4e5f6a1b2')
  if (!found) throw new Error('no fixture image')
  return found
}

async function open(item: MediaView) {
  const view = renderPage(<StorefrontPicture slug={GALLERY_SLUG} item={item} />)
  await view.user.click(screen.getByRole('button', { name: 'Storefront…' }))
  const dialog = await screen.findByRole('dialog', { name: 'Show on the storefront' })
  return { ...view, dialog }
}

describe('StorefrontPicture (#6213)', () => {
  it('adds the image to each chosen listing; a published one is published again', async () => {
    const live = await api.createStorefrontListing(body('Coaster', ['a1b2c3d4e5f6']))
    await api.publishStorefrontListing(live.id)
    const draft = await api.createStorefrontListing(body('Coaster set'))
    const item = await image()
    const { user, dialog } = await open(item)

    const checks = await within(dialog).findAllByRole('checkbox')
    expect(checks.map((c) => c.getAttribute('aria-checked'))).toEqual(['false', 'false'])
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()
    await user.click(within(dialog).getByRole('button', { name: 'Choose all' }))
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    expect(await within(dialog).findByText('Added; live now.')).toBeInTheDocument()
    expect(within(dialog).getByText('Added to the draft.')).toBeInTheDocument()
    const published = await api.storefrontListing(live.id)
    expect(published.published).toBe(true)
    expect(published.media_ids).toEqual(['a1b2c3d4e5f6', item.id])
    expect(published.media.map((m) => m.media_id)).toEqual(['a1b2c3d4e5f6', item.id])
    const still = await api.storefrontListing(draft.id)
    expect([still.published, still.media_ids]).toEqual([false, [item.id]])
    // Nothing left to save.
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('takes the image off a listing that shows it', async () => {
    const item = await image()
    const listing = await api.createStorefrontListing(body('Coaster', [item.id, 'a1b2c3d4e5f6']))
    const { user, dialog } = await open(item)

    const check = await within(dialog).findByRole('checkbox', { name: 'Show on Coaster' })
    expect(check).toHaveAttribute('aria-checked', 'true')
    await user.click(check)
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    expect(await within(dialog).findByText('Taken off the draft.')).toBeInTheDocument()
    await waitFor(async () => expect((await api.storefrontListing(listing.id)).media_ids).toEqual(['a1b2c3d4e5f6']))
  })

  it('reads a listing changed elsewhere again, so a second Save goes through', async () => {
    const item = await image()
    const listing = await api.createStorefrontListing(body('Coaster'))
    const { user, dialog } = await open(item)
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Show on Coaster' }))
    // Edited elsewhere after the dialog read it.
    await api.setStorefrontListingMedia(listing.id, ['a1b2c3d4e5f6'], listing.updated_at)

    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(await within(dialog).findByText(/Changed elsewhere since it was read/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    expect(await within(dialog).findByText('Added to the draft.')).toBeInTheDocument()
    expect((await api.storefrontListing(listing.id)).media_ids).toEqual(['a1b2c3d4e5f6', item.id])
  })

  it('says the pictures were saved when only the republish was refused', async () => {
    const item = await image()
    const listing = await api.createStorefrontListing(body('Coaster', ['a1b2c3d4e5f6']))
    const live = await api.publishStorefrontListing(listing.id)
    refuseNextRepublish('listing_colour_unknown')
    const { user, dialog } = await open(item)
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Show on Coaster' }))
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    expect(await within(dialog).findByText(/^Pictures saved, but not published again: .*listing_colour_unknown.*The store shows the last published pictures/)).toBeInTheDocument()
    // Nothing is left to save: the dialog holds the listing as it now is.
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()
    const after = await api.storefrontListing(listing.id)
    expect(after.media_ids).toEqual(['a1b2c3d4e5f6', item.id])
    expect(after.media).toEqual(live.media)
  })

  it('shows the refusal of a picture that is no image', async () => {
    const video = ((await api.getModel(GALLERY_SLUG)).media ?? []).find((m) => m.kind === 'video')
    if (!video) throw new Error('no fixture video')
    await api.createStorefrontListing(body('Coaster'))
    const { user, dialog } = await open(video)
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Show on Coaster' }))
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(await within(dialog).findByText(/Not saved: .*not an image/)).toBeInTheDocument()
  })

  it('says when no listing sells the design', async () => {
    const { dialog } = await open(await image())
    expect(await within(dialog).findByText(/No listing sells this design yet/)).toBeInTheDocument()
  })
})
