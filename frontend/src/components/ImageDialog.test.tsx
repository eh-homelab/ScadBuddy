import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { ModelSummary } from '../api/types'
import { BUILTIN_SLUG, GALLERY_SLUG } from '../mocks/fixtures'
import { ImageDialog } from './ImageDialog'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ImageDialog', () => {
  function setup(model?: ModelSummary, onMediaChanged = vi.fn()) {
    const captureImage = vi.fn(async () => new Blob(['png'], { type: 'image/png' }))
    URL.createObjectURL = vi.fn(() => 'blob:preview')
    URL.revokeObjectURL = vi.fn()
    render(
      <ImageDialog
        open
        slug={model?.slug ?? 'name-puzzle'}
        captureImage={captureImage}
        viewSize={() => ({ width: 800, height: 500 })}
        model={model}
        onMediaChanged={onMediaChanged}
        onClose={() => undefined}
      />,
    )
    return captureImage
  }

  // Spied, as the multipart upload cannot cross jsdom into Node's fetch.
  function mockUploadMedia() {
    return vi.spyOn(api, 'uploadMedia').mockImplementation(async (slug, file) => {
      const model = await api.getModel(slug)
      const added = {
        id: 'f00dfeedbeef',
        file: 'f00dfeedbeef.png',
        kind: 'image' as const,
        caption: '',
        poster: null,
        missing: false,
        content_type: 'image/png',
        size: file.size,
      }
      return { ...model, media: [...(model.media ?? []), added] }
    })
  }

  it('offers sizes from the view and previews the choices', async () => {
    const captureImage = setup()
    expect(screen.getByText('1600 × 1000')).toBeInTheDocument()
    expect(screen.getByText('3200 × 2000')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('image-preview')).toBeInTheDocument())
    expect(captureImage).toHaveBeenLastCalledWith({ scale: 1, plate: true, transparent: false })

    fireEvent.click(screen.getByTestId('image-plate'))
    await waitFor(() =>
      expect(captureImage).toHaveBeenLastCalledWith({ scale: 1, plate: false, transparent: false }),
    )
  })

  it('saves the image at the chosen size, without the plate', async () => {
    const captureImage = setup()
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    fireEvent.click(screen.getByLabelText(/4×/))
    fireEvent.click(screen.getByTestId('image-plate'))
    fireEvent.click(screen.getByTestId('image-save'))
    await waitFor(() => expect(click).toHaveBeenCalled())
    expect(captureImage).toHaveBeenLastCalledWith({ scale: 4, plate: false, transparent: false })
    click.mockRestore()
  })

  it("adds the image to the template's media", async () => {
    const upload = mockUploadMedia()
    const reorder = vi.spyOn(api, 'reorderMedia')
    const onMediaChanged = vi.fn()
    setup(await api.getModel(GALLERY_SLUG), onMediaChanged)

    fireEvent.click(screen.getByTestId('image-add-media'))
    expect(await screen.findByText('Added to the media')).toBeInTheDocument()
    expect(upload).toHaveBeenCalledWith(
      GALLERY_SLUG,
      expect.objectContaining({ name: expect.stringMatching(/^render-\d{8}-\d{6}\.png$/) }),
    )
    expect(reorder).not.toHaveBeenCalled()
    expect(onMediaChanged.mock.calls[0]?.[0].media.at(-1).id).toBe('f00dfeedbeef')
  })

  it('adds it as the cover: first in the media', async () => {
    mockUploadMedia()
    const reorder = vi
      .spyOn(api, 'reorderMedia')
      .mockImplementation(async (slug) => await api.getModel(slug))
    setup(await api.getModel(GALLERY_SLUG))

    fireEvent.click(screen.getByTestId('image-add-cover'))
    expect(await screen.findByText('Added to the media as the cover')).toBeInTheDocument()
    expect(reorder).toHaveBeenCalledWith(GALLERY_SLUG, [
      'f00dfeedbeef',
      'a1b2c3d4e5f6',
      'b2c3d4e5f6a1',
      'c3d4e5f6a1b2',
      'd4e5f6a1b2c3',
    ])
  })

  it('says how to allow the clipboard when the site was denied it (#722)', async () => {
    vi.stubGlobal('ClipboardItem', class {})
    vi.stubGlobal('navigator', {
      ...navigator,
      clipboard: { write: vi.fn().mockRejectedValue(new DOMException('no', 'NotAllowedError')) },
      permissions: { query: vi.fn().mockResolvedValue({ state: 'denied' }) },
    })
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    setup()
    fireEvent.click(screen.getByTestId('image-copy'))
    expect(await screen.findByRole('alert')).toHaveTextContent(/allow the clipboard for it/i)
    vi.unstubAllGlobals()
  })

  it("cannot add to a built-in's media", async () => {
    setup(await api.getModel(BUILTIN_SLUG))
    expect(screen.getByTestId('image-add-media')).toBeDisabled()
    expect(screen.getByTestId('image-add-cover')).toBeDisabled()
    expect(screen.getByText(/Duplicate it to keep images with it/)).toBeInTheDocument()
  })
})
