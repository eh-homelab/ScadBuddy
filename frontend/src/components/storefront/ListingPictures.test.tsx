import { screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { MediaView, ModelSummary } from '../../api/types'
import { GALLERY_SLUG, MEDIA_PNG_BASE64 } from '../../mocks/fixtures'
import { renderPage } from '../../test/utils'
import { ListingPictures, type LivePictures } from './ListingPictures'

const PNG = Uint8Array.from(atob(MEDIA_PNG_BASE64), (c) => c.charCodeAt(0))

function Harness({ media, start, live, seen, uploaded }: { media: MediaView[]; start: string[]; live: LivePictures; seen: string[][]; uploaded?: (model: ModelSummary, id: string) => void }) {
  const [chosen, setChosen] = useState(start)
  const [items, setItems] = useState(media)
  return (
    <ListingPictures
      slug={GALLERY_SLUG}
      media={items}
      chosen={chosen}
      live={live}
      onChange={(ids) => {
        seen.push(ids)
        setChosen(ids)
      }}
      // As the page does: the id goes onto the pictures as they are when the upload ends.
      onUploaded={(model, id) => {
        setItems(model.media ?? [])
        setChosen((cur) => {
          const next = [...cur, id]
          seen.push(next)
          return next
        })
        uploaded?.(model, id)
      }}
    />
  )
}

function shownNames() {
  return within(screen.getByRole('list', { name: 'Pictures shown' }))
    .getAllByTestId('listing-picture')
    .map((row) => row.querySelector('img')?.getAttribute('alt'))
}

afterEach(() => vi.restoreAllMocks())

describe('ListingPictures (#6213)', () => {
  it('orders, removes and adds the pictures, and offers no video', async () => {
    const media = (await api.getModel(GALLERY_SLUG)).media ?? []
    const seen: string[][] = []
    const { user } = renderPage(<Harness media={media} start={['a1b2c3d4e5f6', 'b2c3d4e5f6a1']} live={null} seen={seen} />)
    expect(shownNames()).toEqual(['Printed in blue and orange', 'The raised rim'])
    // Not published: no Live marks.
    expect(screen.queryByText('Live')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Move The raised rim up' }))
    expect(seen.at(-1)).toEqual(['b2c3d4e5f6a1', 'a1b2c3d4e5f6'])

    await user.click(screen.getByRole('button', { name: 'Remove Printed in blue and orange' }))
    expect(seen.at(-1)).toEqual(['b2c3d4e5f6a1'])

    const toAdd = screen.getByRole('list', { name: 'Pictures to add' })
    // The video is no picture a customer sees.
    expect(within(toAdd).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual(['Add Printed in blue and orange', 'Add c3d4e5f6a1b2.png'])
    await user.click(within(toAdd).getByRole('button', { name: 'Add c3d4e5f6a1b2.png' }))
    expect(seen.at(-1)).toEqual(['b2c3d4e5f6a1', 'c3d4e5f6a1b2'])
  })

  it('marks each picture live or not yet', async () => {
    const media = (await api.getModel(GALLERY_SLUG)).media ?? []
    renderPage(<Harness media={media} start={['a1b2c3d4e5f6', 'b2c3d4e5f6a1']} live={['a1b2c3d4e5f6']} seen={[]} />)
    const [first, second] = screen.getAllByTestId('listing-picture')
    expect(within(first!).getByText('Live')).toBeInTheDocument()
    expect(within(second!).getByText('Not live yet')).toBeInTheDocument()
    expect(screen.getByText(/Save draft takes the listing off the store/)).toBeInTheDocument()
  })

  it('marks nothing when the published pictures cannot be told apart, and says to publish again', async () => {
    const media = (await api.getModel(GALLERY_SLUG)).media ?? []
    renderPage(<Harness media={media} start={['a1b2c3d4e5f6']} live="unknown" seen={[]} />)
    expect(screen.getByText('Publish again to see which pictures are live.')).toBeInTheDocument()
    expect(screen.queryByText('Live')).not.toBeInTheDocument()
    expect(screen.queryByText('Not live yet')).not.toBeInTheDocument()
  })

  it('uploads a picture into the design’s media and shows it last', async () => {
    const media = (await api.getModel(GALLERY_SLUG)).media ?? []
    const added: MediaView = { id: 'f6a1b2c3d4e5', file: 'f6a1b2c3d4e5.png', kind: 'image', caption: '', poster: null, missing: false, readonly: false, content_type: 'image/png', size: PNG.length }
    const upload = vi.spyOn(api, 'uploadMedia').mockImplementation(async (slug) => {
      const model = await api.getModel(slug)
      return { ...model, media: [...(model.media ?? []), added] }
    })
    const seen: string[][] = []
    const uploaded = vi.fn()
    const { user } = renderPage(<Harness media={media} start={['a1b2c3d4e5f6']} live={null} seen={seen} uploaded={uploaded} />)
    const photo = new File([PNG], 'photo.png', { type: 'image/png' })

    await user.upload(screen.getByLabelText('Upload a picture'), photo)

    await waitFor(() => expect(seen.at(-1)).toEqual(['a1b2c3d4e5f6', 'f6a1b2c3d4e5']))
    expect(upload).toHaveBeenCalledWith(GALLERY_SLUG, photo)
    expect(uploaded).toHaveBeenCalledWith(expect.objectContaining({ media: expect.arrayContaining([added]) }), 'f6a1b2c3d4e5')
    expect(shownNames()).toEqual(['Printed in blue and orange', 'f6a1b2c3d4e5.png'])
  })

  it('says so when an upload adds no new picture', async () => {
    const media = (await api.getModel(GALLERY_SLUG)).media ?? []
    vi.spyOn(api, 'uploadMedia').mockImplementation(async (slug) => api.getModel(slug))
    const uploaded = vi.fn()
    const { user } = renderPage(<Harness media={media} start={[]} live={null} seen={[]} uploaded={uploaded} />)
    await user.upload(screen.getByLabelText('Upload a picture'), new File([PNG], 'same.png', { type: 'image/png' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('same.png added no new picture to the design')
    expect(uploaded).not.toHaveBeenCalled()
  })

  it('refuses a video before sending it', async () => {
    const upload = vi.spyOn(api, 'uploadMedia')
    const { user } = renderPage(<Harness media={[]} start={[]} live={null} seen={[]} />, { userEventOptions: { applyAccept: false } })
    await user.upload(screen.getByLabelText('Upload a picture'), new File(['x'], 'clip.mp4', { type: 'video/mp4' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('clip.mp4 is not a PNG, JPEG or WebP image.')
    expect(upload).not.toHaveBeenCalled()
  })
})
