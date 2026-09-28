import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { MediaView, ModelSummary } from '../../api/types'
import { BUILTIN_SLUG, GALLERY_SLUG, MEDIA_PNG_BASE64 } from '../../mocks/fixtures'
import { setMockMedia, setMockUploadLimit } from '../../mocks/handlers'
import { renderPage } from '../../test/utils'
import { MediaManager } from './MediaManager'

const MiB = 1024 * 1024
const PNG = Uint8Array.from(atob(MEDIA_PNG_BASE64), (c) => c.charCodeAt(0))

async function render(slug = GALLERY_SLUG) {
  const model = await api.getModel(slug)
  const onChanged = vi.fn<(model: ModelSummary) => void>()
  const view = renderPage(<MediaManager model={model} onChanged={onChanged} />, {
    userEventOptions: { applyAccept: false },
  })
  return { ...view, model, onChanged }
}

/** The ids of the items as the manager lists them, in order. */
function shownIds() {
  const list = screen.getByRole('list', { name: 'Media items' })
  return within(list)
    .getAllByRole('listitem')
    .map((item) => item.getAttribute('data-media-id'))
}

function item(index: number) {
  const list = screen.getByRole('list', { name: 'Media items' })
  return within(list).getAllByRole('listitem')[index]!
}

async function serverIds(slug = GALLERY_SLUG) {
  return ((await api.getModel(slug)).media ?? []).map((entry) => entry.id)
}

const GALLERY = ['a1b2c3d4e5f6', 'b2c3d4e5f6a1', 'c3d4e5f6a1b2', 'd4e5f6a1b2c3']

function missingVideo(): MediaView {
  return {
    id: 'e5f6a1b2c3d4',
    file: 'e5f6a1b2c3d4.mp4',
    kind: 'video',
    caption: 'Lost with the volume',
    poster: null,
    missing: true,
    content_type: 'video/mp4',
    size: null,
  }
}

afterEach(() => vi.restoreAllMocks())

describe('MediaManager (#279)', () => {
  it('lists the media in order, the first marked as the cover', async () => {
    await render()
    expect(shownIds()).toEqual(GALLERY)
    expect(within(item(0)).getByText('Cover')).toBeInTheDocument()
    expect(within(item(1)).queryByText('Cover')).not.toBeInTheDocument()
    expect(within(item(0)).queryByRole('button', { name: 'Make cover' })).not.toBeInTheDocument()
    expect(within(item(0)).getByRole('button', { name: 'Move up' })).toBeDisabled()
    expect(within(item(3)).getByRole('button', { name: 'Move down' })).toBeDisabled()
  })

  it('adds a chosen file as the last item, showing its progress', async () => {
    let finish: () => void = () => {}
    const gate = new Promise<void>((resolve) => (finish = resolve))
    const upload = vi
      .spyOn(api, 'uploadMedia')
      .mockImplementation(async (slug, file, _options, onProgress) => {
        onProgress?.(0.5)
        await gate
        const model = await api.getModel(slug)
        const added: MediaView = {
          id: 'f6a1b2c3d4e5',
          file: 'f6a1b2c3d4e5.png',
          kind: 'image',
          caption: '',
          poster: null,
          missing: false,
          content_type: 'image/png',
          size: file.size,
        }
        return { ...model, media: [...(model.media ?? []), added] }
      })
    const { user, onChanged } = await render()
    const photo = new File([PNG], 'photo.png', { type: 'image/png' })

    await user.upload(screen.getByLabelText('Add images or videos'), photo)

    const bar = await screen.findByRole('progressbar', { name: 'Uploading photo.png' })
    await waitFor(() => expect(bar).toHaveAttribute('aria-valuenow', '50'))
    finish()
    await waitFor(() => expect(shownIds()).toEqual([...GALLERY, 'f6a1b2c3d4e5']))
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(upload).toHaveBeenCalledWith(GALLERY_SLUG, photo, {}, expect.any(Function))
    expect(onChanged).toHaveBeenLastCalledWith(
      expect.objectContaining({ media: expect.arrayContaining([expect.objectContaining({ id: 'f6a1b2c3d4e5' })]) }),
    )
  })

  it('uploads several dropped files one after another, in the order dropped', async () => {
    const order: string[] = []
    vi.spyOn(api, 'uploadMedia').mockImplementation(async (slug, file) => {
      order.push(file.name)
      return api.getModel(slug)
    })
    await render()
    const first = new File([PNG], 'one.png', { type: 'image/png' })
    const second = new File([PNG], 'two.webm', { type: 'video/webm' })

    fireEvent.drop(screen.getByTestId('media-dropzone'), {
      dataTransfer: { files: [first, second], items: [], types: ['Files'] },
    })

    await waitFor(() => expect(order).toEqual(['one.png', 'two.webm']))
  })

  it('refuses a file over the upload limit before sending it', async () => {
    setMockUploadLimit(MiB)
    const upload = vi.spyOn(api, 'uploadMedia')
    const { user } = await render()
    const clip = new File([new Uint8Array(MiB + 1)], 'clip.mp4', { type: 'video/mp4' })

    await user.upload(screen.getByLabelText('Add images or videos'), clip)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'clip.mp4 is larger than the 1 MB upload limit',
    )
    expect(upload).not.toHaveBeenCalled()
    expect(shownIds()).toEqual(GALLERY)
  })

  it('refuses an image over 10 MB and a file that is neither image nor video', async () => {
    const upload = vi.spyOn(api, 'uploadMedia')
    const { user } = await render()
    const big = new File([new Uint8Array(10 * MiB + 1)], 'big.png', { type: 'image/png' })
    const notes = new File(['hello'], 'notes.txt', { type: 'text/plain' })

    await user.upload(screen.getByLabelText('Add images or videos'), [big, notes])

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('big.png is larger than the 10 MB limit for images')
    expect(alert).toHaveTextContent('notes.txt is not a PNG, JPEG or WebP image, or an MP4 or WebM video')
    expect(upload).not.toHaveBeenCalled()
  })

  it('moves an item with Move down and Move up', async () => {
    const { user } = await render()

    await user.click(within(item(0)).getByRole('button', { name: 'Move down' }))
    await waitFor(() => expect(shownIds()).toEqual([GALLERY[1], GALLERY[0], GALLERY[2], GALLERY[3]]))
    expect(await serverIds()).toEqual([GALLERY[1], GALLERY[0], GALLERY[2], GALLERY[3]])

    await user.click(within(item(3)).getByRole('button', { name: 'Move up' }))
    await waitFor(() => expect(shownIds()).toEqual([GALLERY[1], GALLERY[0], GALLERY[3], GALLERY[2]]))
    expect(await serverIds()).toEqual([GALLERY[1], GALLERY[0], GALLERY[3], GALLERY[2]])
  })

  it('reorders by drag and drop', async () => {
    const { onChanged } = await render()

    fireEvent.dragStart(item(2))
    fireEvent.dragOver(item(0))
    fireEvent.drop(item(0))

    await waitFor(() => expect(shownIds()).toEqual([GALLERY[2], GALLERY[0], GALLERY[1], GALLERY[3]]))
    expect(await serverIds()).toEqual([GALLERY[2], GALLERY[0], GALLERY[1], GALLERY[3]])
    expect(onChanged).toHaveBeenCalled()
  })

  it('makes an item the cover by moving it first', async () => {
    const { user } = await render()

    await user.click(within(item(3)).getByRole('button', { name: 'Make cover' }))

    await waitFor(() => expect(shownIds()).toEqual([GALLERY[3], GALLERY[0], GALLERY[1], GALLERY[2]]))
    expect(within(item(0)).getByText('Cover')).toBeInTheDocument()
    expect(await serverIds()).toEqual([GALLERY[3], GALLERY[0], GALLERY[1], GALLERY[2]])
  })

  it('saves a caption when the field loses focus, not on every key', async () => {
    const patch = vi.spyOn(api, 'patchMedia')
    const { user } = await render()
    const field = within(item(1)).getByRole('textbox', { name: 'Caption' })
    expect(field).toHaveValue('The raised rim')

    await user.clear(field)
    await user.type(field, 'Rim detail')
    expect(patch).not.toHaveBeenCalled()

    await user.tab()
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1))
    expect(patch).toHaveBeenCalledWith(GALLERY_SLUG, GALLERY[1], 'Rim detail')
    await waitFor(async () =>
      expect((await api.getModel(GALLERY_SLUG)).media?.[1]?.caption).toBe('Rim detail'),
    )
  })

  it('does not save a caption left as it was', async () => {
    const patch = vi.spyOn(api, 'patchMedia')
    const { user } = await render()

    await user.click(within(item(0)).getByRole('textbox', { name: 'Caption' }))
    await user.tab()

    expect(patch).not.toHaveBeenCalled()
  })

  it('deletes an item only once confirmed', async () => {
    const { user } = await render()

    await user.click(within(item(0)).getByRole('button', { name: 'Delete' }))
    await user.click(within(item(0)).getByRole('button', { name: 'Cancel' }))
    expect(shownIds()).toEqual(GALLERY)

    await user.click(within(item(0)).getByRole('button', { name: 'Delete' }))
    expect(within(item(0)).getByText(/Delete this image\?/)).toBeInTheDocument()
    await user.click(within(item(0)).getByRole('button', { name: 'Yes, delete' }))

    await waitFor(() => expect(shownIds()).toEqual(GALLERY.slice(1)))
    expect(await serverIds()).toEqual(GALLERY.slice(1))
  })

  it('shows an item whose file is gone as missing, with a way to remove it', async () => {
    const gallery = (await api.getModel(GALLERY_SLUG)).media ?? []
    setMockMedia(GALLERY_SLUG, [...gallery, missingVideo()])
    const { user } = await render()

    const missing = item(4)
    expect(missing).toHaveTextContent('File missing')
    expect(within(missing).queryByRole('textbox', { name: 'Caption' })).not.toBeInTheDocument()
    expect(within(missing).queryByRole('img')).not.toBeInTheDocument()

    await user.click(within(missing).getByRole('button', { name: 'Remove' }))

    await waitFor(() => expect(shownIds()).toEqual(GALLERY))
    expect(await serverIds()).toEqual(GALLERY)
  })

  it('shows a built-in media read-only, and offers Duplicate instead', async () => {
    const gallery = (await api.getModel(GALLERY_SLUG)).media ?? []
    setMockMedia(BUILTIN_SLUG, gallery)
    await render(BUILTIN_SLUG)

    expect(shownIds()).toEqual(GALLERY)
    expect(screen.getByText(/Built-in media is read-only/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Duplicate' })).toBeInTheDocument()
    for (const name of ['Move up', 'Move down', 'Make cover', 'Delete', 'Remove']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
    expect(screen.queryByRole('textbox', { name: 'Caption' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Add images or videos')).not.toBeInTheDocument()
    expect(screen.queryByTestId('media-dropzone')).not.toBeInTheDocument()
    expect(item(0).getAttribute('draggable')).not.toBe('true')
    expect(screen.getByText('Printed in blue and orange')).toBeInTheDocument()
  })

  it('says why a write failed and keeps the list as it was', async () => {
    vi.spyOn(api, 'reorderMedia').mockRejectedValue(new Error('the database is down'))
    const { user } = await render()

    await user.click(within(item(0)).getByRole('button', { name: 'Move down' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('the database is down')
    expect(shownIds()).toEqual(GALLERY)
  })
})
