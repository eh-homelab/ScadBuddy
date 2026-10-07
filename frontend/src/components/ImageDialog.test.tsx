import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { ModelSummary } from '../api/types'
import type { CameraView } from '../lib/framing'
import type { SnapshotOptions } from '../lib/snapshot'
import { BUILTIN_SLUG, GALLERY_SLUG } from '../mocks/fixtures'
import { setMockMedia } from '../mocks/handlers'
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
        readonly: false,
        content_type: 'image/png',
        size: file.size,
      }
      return { ...model, media: [...(model.media ?? []), added] }
    })
  }

  it('releases the preview when it unmounts while open', async () => {
    const captureImage = vi.fn(async () => new Blob(['png'], { type: 'image/png' }))
    URL.createObjectURL = vi.fn(() => 'blob:preview')
    const revoke = vi.fn()
    URL.revokeObjectURL = revoke
    const view = render(
      <ImageDialog
        open
        slug="name-puzzle"
        captureImage={captureImage}
        viewSize={() => ({ width: 800, height: 500 })}
        onClose={() => undefined}
      />,
    )
    await waitFor(() => expect(screen.getByTestId('image-preview')).toBeInTheDocument())
    expect(revoke).not.toHaveBeenCalled()

    view.unmount()

    expect(revoke).toHaveBeenCalledWith('blob:preview')
  })

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
    // Not the last call: unticking the plate also redraws the preview at 1× on the next
    // animation frame, which can land after the save's own draw.
    expect(captureImage).toHaveBeenCalledWith({ scale: 4, plate: false, transparent: false })
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

  describe('framing (#722)', () => {
    const CAMERA: CameraView = { position: [0, 100, 200], target: [0, 20, 0], fov: 35 }

    function frame() {
      const captureImage = vi.fn(async (_options: SnapshotOptions) => new Blob(['png'], { type: 'image/png' }))
      const cameraView = vi.fn(() => ({ ...CAMERA }))
      URL.createObjectURL = vi.fn(() => 'blob:preview')
      URL.revokeObjectURL = vi.fn()
      render(
        <ImageDialog
          open
          slug="name-puzzle"
          captureImage={captureImage}
          viewSize={() => ({ width: 800, height: 500 })}
          cameraView={cameraView}
          onClose={() => undefined}
        />,
      )
      const last = () => captureImage.mock.calls.at(-1)?.[0].view
      return { captureImage, cameraView, last }
    }

    it("previews the viewer's camera, and a drag turns only the dialog's copy", async () => {
      const { captureImage, cameraView, last } = frame()
      await waitFor(() => expect(last()).toEqual(CAMERA))

      const surface = screen.getByTestId('image-framing')
      fireEvent.pointerDown(surface, { pointerId: 1, clientX: 100, clientY: 100, button: 0 })
      fireEvent.pointerMove(surface, { pointerId: 1, clientX: 160, clientY: 100 })
      fireEvent.pointerUp(surface, { pointerId: 1 })

      await waitFor(() => expect(last()?.position).not.toEqual(CAMERA.position))
      expect(last()?.target).toEqual(CAMERA.target)
      // The viewer's camera was read once, as the dialog opened, and never written.
      expect(cameraView).toHaveBeenCalledTimes(1)

      const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
      fireEvent.click(screen.getByTestId('image-save'))
      await waitFor(() => expect(click).toHaveBeenCalled())
      const saved = captureImage.mock.calls.find(([options]) => options.scale === 2)?.[0]
      expect(saved?.view?.position).not.toEqual(CAMERA.position)
    })

    it('pans with Shift-drag and zooms with the wheel', async () => {
      const { last } = frame()
      await waitFor(() => expect(last()).toEqual(CAMERA))
      const surface = screen.getByTestId('image-framing')

      fireEvent.pointerDown(surface, { pointerId: 1, clientX: 100, clientY: 100, shiftKey: true })
      fireEvent.pointerMove(surface, { pointerId: 1, clientX: 140, clientY: 100 })
      fireEvent.pointerUp(surface, { pointerId: 1 })
      await waitFor(() => expect(last()?.target).not.toEqual(CAMERA.target))

      const panned = last()!
      fireEvent.wheel(surface, { deltaY: -200 })
      await waitFor(() => expect(last()?.position).not.toEqual(panned.position))
      expect(last()?.target).toEqual(panned.target)
    })

    it('puts the framing back with Reset to view', async () => {
      const { cameraView, last } = frame()
      await waitFor(() => expect(last()).toEqual(CAMERA))
      fireEvent.keyDown(screen.getByTestId('image-framing'), { key: 'ArrowLeft' })
      await waitFor(() => expect(last()?.position).not.toEqual(CAMERA.position))

      fireEvent.click(screen.getByTestId('image-reset-view'))
      await waitFor(() => expect(last()).toEqual(CAMERA))
      expect(cameraView).toHaveBeenCalledTimes(2)
    })

    it("offers sizes in the framing's shape", async () => {
      const { last } = frame()
      expect(screen.getByText('1600 × 1000')).toBeInTheDocument()
      fireEvent.click(screen.getByLabelText('Square'))
      expect(screen.getByText('1600 × 1600')).toBeInTheDocument()
      await waitFor(() => expect(last()?.aspect).toBe(1))
      fireEvent.click(screen.getByLabelText('16:9'))
      expect(screen.getByText('1600 × 900')).toBeInTheDocument()
      fireEvent.click(screen.getByLabelText('As the viewer'))
      expect(screen.getByText('1600 × 1000')).toBeInTheDocument()
      await waitFor(() => expect(last()).toEqual(CAMERA))
    })
  })

  it("adds to a built-in's media, choosing its cover rather than reordering (#722)", async () => {
    setMockMedia(BUILTIN_SLUG, [
      { ...(await api.getModel(GALLERY_SLUG)).media![0]!, id: 'front', readonly: true },
    ])
    mockUploadMedia()
    const reorder = vi.spyOn(api, 'reorderMedia')
    const choose = vi
      .spyOn(api, 'setMediaCover')
      .mockImplementation(async (slug) => await api.getModel(slug))
    setup(await api.getModel(BUILTIN_SLUG))

    expect(screen.queryByText(/cannot change/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('image-add-cover'))
    expect(await screen.findByText('Added to the media as the cover')).toBeInTheDocument()
    expect(choose).toHaveBeenCalledWith(BUILTIN_SLUG, 'f00dfeedbeef')
    expect(reorder).not.toHaveBeenCalled()
  })
})
