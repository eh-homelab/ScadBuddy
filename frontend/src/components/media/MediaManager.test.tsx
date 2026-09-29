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
    readonly: false,
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
          readonly: false,
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

  it("shows a built-in's shipped media read-only, and lets media be added after it (#722)", async () => {
    const gallery = (await api.getModel(GALLERY_SLUG)).media ?? []
    setMockMedia(
      BUILTIN_SLUG,
      gallery.map((entry) => ({ ...entry, readonly: true })),
    )
    await render(BUILTIN_SLUG)

    expect(shownIds()).toEqual(GALLERY)
    expect(screen.getByText(/ships is read-only/)).toBeInTheDocument()
    expect(screen.getAllByText('Shipped')).toHaveLength(GALLERY.length)
    for (const name of ['Move up', 'Move down', 'Delete', 'Remove']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
    expect(screen.queryByRole('textbox', { name: 'Caption' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Add images or videos')).toBeInTheDocument()
    expect(screen.getByTestId('media-dropzone')).toBeInTheDocument()
    expect(item(0).getAttribute('draggable')).not.toBe('true')
    expect(screen.getByText('Printed in blue and orange')).toBeInTheDocument()
  })

  it("edits only a built-in's added media, and chooses its cover apart from the order", async () => {
    const [shipped] = (await api.getModel(GALLERY_SLUG)).media ?? []
    const added = (id: string, caption: string): MediaView => ({
      ...shipped!,
      id,
      file: `${id}.png`,
      caption,
      readonly: false,
    })
    setMockMedia(BUILTIN_SLUG, [
      { ...shipped!, id: 'front', readonly: true },
      added('aaaaaaaaaaaa', 'One'),
      added('bbbbbbbbbbbb', 'Two'),
    ])
    const { user } = await render(BUILTIN_SLUG)

    expect(within(item(0)).queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
    expect(within(item(1)).getByRole('button', { name: 'Move up' })).toBeDisabled()
    await user.click(within(item(1)).getByRole('button', { name: 'Move down' }))
    await waitFor(() => expect(shownIds()).toEqual(['front', 'bbbbbbbbbbbb', 'aaaaaaaaaaaa']))

    await user.click(within(item(2)).getByRole('button', { name: 'Make cover' }))
    await waitFor(() => expect(shownIds()).toEqual(['aaaaaaaaaaaa', 'front', 'bbbbbbbbbbbb']))
    expect(within(item(0)).getByText('Cover')).toBeInTheDocument()
    // The chosen cover is not moved with the rest.
    expect(within(item(0)).queryByRole('button', { name: 'Move up' })).not.toBeInTheDocument()
    expect((await api.getModel(BUILTIN_SLUG)).media_cover).toBe('aaaaaaaaaaaa')

    await user.click(within(item(0)).getByRole('button', { name: 'Use the shipped cover' }))
    await waitFor(() => expect(shownIds()).toEqual(['front', 'bbbbbbbbbbbb', 'aaaaaaaaaaaa']))

    await user.click(within(item(2)).getByRole('button', { name: 'Delete' }))
    await user.click(within(item(2)).getByRole('button', { name: 'Yes, delete' }))
    await waitFor(() => expect(shownIds()).toEqual(['front', 'bbbbbbbbbbbb']))
    expect(await serverIds(BUILTIN_SLUG)).toEqual(['front', 'bbbbbbbbbbbb'])
  })

  it('says why a write failed and keeps the list as it was', async () => {
    vi.spyOn(api, 'reorderMedia').mockRejectedValue(new Error('the database is down'))
    const { user } = await render()

    await user.click(within(item(0)).getByRole('button', { name: 'Move down' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('the database is down')
    expect(shownIds()).toEqual(GALLERY)
  })
})

describe('MediaManager paste (#722)', () => {
  function clipboard(files: File[], asItems = false) {
    return asItems
      ? {
          files: [],
          items: files.map((file) => ({ kind: 'file', type: file.type, getAsFile: () => file })),
          types: ['Files'],
        }
      : { files, items: [], types: ['Files'] }
  }

  function spyUploads() {
    const names: string[] = []
    const upload = vi.spyOn(api, 'uploadMedia').mockImplementation(async (slug, file) => {
      names.push(file.name)
      return api.getModel(slug)
    })
    return { upload, names }
  }

  const shot = () => new File([PNG], 'image.png', { type: 'image/png' })
  const settle = () => new Promise((resolve) => setTimeout(resolve, 20))

  function outsideField() {
    const field = document.createElement('input')
    document.body.appendChild(field)
    field.focus()
    return field
  }

  afterEach(() => {
    for (const extra of document.body.querySelectorAll(':scope > input, :scope > button')) {
      extra.remove()
    }
  })

  it('adds a pasted screenshot as an upload, named for when it was pasted', async () => {
    const { names } = spyUploads()
    await render()
    expect(screen.getByTestId('media-paste-hint')).toHaveTextContent('or paste an image or video')

    fireEvent.paste(screen.getByTestId('media-dropzone'), { clipboardData: clipboard([shot()]) })

    await waitFor(() => expect(names).toHaveLength(1))
    expect(names[0]).toMatch(/^pasted-\d{8}-\d{6}\.png$/)
  })

  it('takes a paste from clipboard items, and keeps a real file name', async () => {
    const { names } = spyUploads()
    await render()
    const clip = new File([PNG], 'clip.webm', { type: 'video/webm' })

    fireEvent.paste(document.body, { clipboardData: clipboard([clip], true) })

    await waitFor(() => expect(names).toEqual(['clip.webm']))
  })

  it('takes a paste anywhere on the page while no text field has focus', async () => {
    const { upload } = spyUploads()
    await render()
    const button = document.createElement('button')
    document.body.appendChild(button)
    button.focus()

    fireEvent.paste(button, { clipboardData: clipboard([shot()]) })

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(1))
  })

  it('leaves a paste into a text field outside the section to that field', async () => {
    const { upload } = spyUploads()
    await render()
    const field = outsideField()

    fireEvent.paste(field, { clipboardData: clipboard([shot()]) })

    await settle()
    expect(upload).not.toHaveBeenCalled()
  })

  it('takes a paste into that field while the pointer is over the section', async () => {
    const { upload } = spyUploads()
    await render()
    const field = outsideField()

    fireEvent.pointerEnter(screen.getByRole('region', { name: 'Media' }))
    fireEvent.paste(field, { clipboardData: clipboard([shot()]) })

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(1))
  })

  it('gives a page-wide paste to the newest of two mounted managers only', async () => {
    const { upload } = spyUploads()
    await render()
    const second = await api.getModel(BUILTIN_SLUG)
    renderPage(<MediaManager model={second} onChanged={vi.fn()} />)

    fireEvent.paste(document.body, { clipboardData: clipboard([shot()]) })

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(1))
    await settle()
    expect(upload).toHaveBeenCalledTimes(1)
    expect(upload.mock.calls[0]?.[0]).toBe(BUILTIN_SLUG)
  })

  it("takes a paste into a caption, since that field is the section's own", async () => {
    const { upload } = spyUploads()
    await render()
    const caption = within(item(0)).getByRole('textbox', { name: 'Caption' })
    caption.focus()

    fireEvent.paste(caption, { clipboardData: clipboard([shot()]) })

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(1))
  })

  it('ignores a paste of plain text', async () => {
    const { upload } = spyUploads()
    await render()

    fireEvent.paste(document.body, {
      clipboardData: {
        files: [],
        items: [{ kind: 'string', type: 'text/plain' }],
        types: ['text/plain'],
      },
    })

    await settle()
    expect(upload).not.toHaveBeenCalled()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('checks a pasted file as it checks an upload', async () => {
    setMockUploadLimit(MiB)
    const { upload } = spyUploads()
    await render()
    const gif = new File(['GIF89a'], 'image.gif', { type: 'image/gif' })
    const clip = new File([new Uint8Array(MiB + 1)], 'clip.mp4', { type: 'video/mp4' })

    fireEvent.paste(document.body, { clipboardData: clipboard([gif, clip]) })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/is not a PNG, JPEG or WebP image/)
    expect(alert).toHaveTextContent('clip.mp4 is larger than the 1 MB upload limit')
    expect(upload).not.toHaveBeenCalled()
  })

  it("takes a paste into a built-in's media, after what it ships (#722)", async () => {
    const { upload, names } = spyUploads()
    await render(BUILTIN_SLUG)
    expect(screen.getByTestId('media-paste-hint')).toBeInTheDocument()

    fireEvent.paste(document.body, { clipboardData: clipboard([shot()]) })

    await waitFor(() => expect(names).toHaveLength(1))
    expect(upload).toHaveBeenCalledWith(BUILTIN_SLUG, expect.any(File), {}, expect.any(Function))
  })
})
