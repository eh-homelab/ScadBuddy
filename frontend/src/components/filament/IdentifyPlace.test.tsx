import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { L } from '../../mocks/features/filament/seed'
import { queueScanCodes } from '../../mocks/features/scan'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { installNdefStub, type NdefStub } from '../../test/ndefStub'
import { renderPage } from '../../test/utils'
import { IdentifyPlace } from './IdentifyPlace'

type Nfc = 'ok' | 'no_api' | 'embedded'

function show(nfc: Nfc = 'ok') {
  const onIdentified = vi.fn()
  const view = renderPage(<IdentifyPlace onIdentified={onIdentified} nfc={nfc} />)
  return { ...view, onIdentified }
}

/** The places named so far, without the clears (`null`) between them. */
const identified = (fn: ReturnType<typeof vi.fn>) => fn.mock.calls.map((call) => call[0] as { id: string } | null).filter((one) => one !== null)

const last = (fn: ReturnType<typeof vi.fn>) => fn.mock.calls.at(-1)?.[0] as { id: string } | null | undefined

/** A stand-in for BarcodeDetector, and a camera that gives a stream. */
function installCamera(rawValue: string) {
  const stop = vi.fn()
  const stream = { getTracks: () => [{ stop }] }
  const before = navigator.mediaDevices
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia: vi.fn(() => Promise.resolve(stream)) }, configurable: true })
  ;(window as unknown as { BarcodeDetector: unknown }).BarcodeDetector = class {
    detect() {
      return Promise.resolve([{ rawValue, format: 'qr_code' }])
    }
  }
  return {
    stop,
    restore() {
      delete (window as unknown as { BarcodeDetector?: unknown }).BarcodeDetector
      Object.defineProperty(navigator, 'mediaDevices', { value: before, configurable: true })
    },
  }
}

const urlRecord = (url: string) => ({ recordType: 'url', data: new TextEncoder().encode(url) })

describe('IdentifyPlace (#7168)', () => {
  beforeEach(() => {
    resetMockState()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('keeps the no-label sentence for a photo whose only code names no ScadBuddy thing (#8372)', async () => {
    // jsdom draws nothing: a canvas that "draws" and makes a small JPEG, and a photo it can open.
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: () => undefined } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function (callback: BlobCallback) {
      callback(new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], { type: 'image/jpeg' }))
    })
    vi.stubGlobal('createImageBitmap', async () => ({ width: 4000, height: 3000, close: () => undefined }))
    try {
      queueScanCodes([{ format: 'ean_13', text: '6975373321234' }])
      const { user, onIdentified } = show('no_api')
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      await user.upload(screen.getByLabelText('Photo of the label'), new File([new Uint8Array([0xff, 0xd8])], 'label.jpg', { type: 'image/jpeg' }))
      expect(await screen.findByText('No place label was found in the photo. Take it again, or type the code.')).toBeInTheDocument()
      expect(screen.getByLabelText('Place code')).toBeInTheDocument()
      expect(identified(onIdentified)).toHaveLength(0)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('offers the ways with none chosen', () => {
    show()
    const ways = screen.getByRole('radiogroup', { name: 'How do you name the place?' })
    for (const name of ['Scan tag', 'QR code', 'Pick by hand']) expect(within(ways).getByRole('radio', { name })).toHaveAttribute('aria-checked', 'false')
  })

  describe('with Web NFC', () => {
    let stub: NdefStub
    beforeEach(() => {
      stub = installNdefStub()
    })
    afterEach(() => stub.restore())

    it("names the place a tag's label URL names", async () => {
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
      await screen.findByText("Hold the place's tag to the phone.")
      await waitFor(() => expect(stub.scanning).toBe(true))
      stub.tap({ serialNumber: '04:33:33:33:33:33:33', records: [urlRecord('https://sb.example/x/L-3')] })
      await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
      expect(identified(onIdentified)[0]).toMatchObject({ id: L.DRY_BOX, code: 'L-3' })
    })

    it('names the place of the newest tap when an older read answers last (review 6)', async () => {
      let release!: () => void
      const held = new Promise<void>((resolve) => (release = resolve))
      server.use(
        http.post('/api/v1/filament/tags/reads', async ({ request }) => {
          const body = (await request.clone().json()) as { uid: string }
          if (body.uid === '04:33:33:33:33:33:33') await held
          return undefined
        }),
      )
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
      await waitFor(() => expect(stub.scanning).toBe(true))
      stub.tap({ serialNumber: '04:33:33:33:33:33:33', records: [urlRecord('https://sb.example/x/L-3')] })
      stub.tap({ serialNumber: '04:44:44:44:44:44:44', records: [urlRecord('https://sb.example/x/L-4')] })
      await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
      expect(identified(onIdentified)[0]).toMatchObject({ id: L.AMS_A1 })
      release()
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(identified(onIdentified)).toHaveLength(1)
      expect(last(onIdentified)).toMatchObject({ id: L.AMS_A1 })
    })

    it("names no place for a spool's tag", async () => {
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
      await waitFor(() => expect(stub.scanning).toBe(true))
      stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
      expect(await screen.findByText("This tag is a spool's tag.")).toBeInTheDocument()
      expect(identified(onIdentified)).toHaveLength(0)
    })
  })

  it("names the place a label's QR code names", async () => {
    const camera = installCamera('https://sb.example/x/L-4')
    try {
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      await user.click(screen.getByRole('button', { name: 'Use camera' }))
      await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
      expect(identified(onIdentified)[0]).toMatchObject({ id: L.AMS_A1, code: 'L-4' })
      await waitFor(() => expect(camera.stop).toHaveBeenCalled())
    } finally {
      camera.restore()
    }
  })

  it('says a typed spool code is a spool', async () => {
    const { user, onIdentified } = show()
    await user.click(screen.getByRole('radio', { name: 'QR code' }))
    await user.type(screen.getByLabelText('Place code'), 'S-12{Enter}')
    expect(await screen.findByText('S-12 is a spool, not a place.')).toBeInTheDocument()
    expect(identified(onIdentified)).toHaveLength(0)
  })

  it('refuses an archived place and an AMS unit', async () => {
    server.use(http.get('/api/v1/codes/L-2', () => HttpResponse.json({ kind: 'location', id: L.SHELF_A, code: 'L-2', name: 'Shelf A', archived: true })))
    const { user, onIdentified } = show()
    await user.click(screen.getByRole('radio', { name: 'QR code' }))
    const field = screen.getByLabelText('Place code')
    await user.type(field, 'L-2{Enter}')
    expect(await screen.findByText('L-2 is archived.')).toBeInTheDocument()
    await user.clear(field)
    await user.type(field, 'l6{Enter}')
    expect(await screen.findByText('L-6 is an AMS unit. Choose one of its slots, or another place.')).toBeInTheDocument()
    expect(identified(onIdentified)).toHaveLength(0)
  })

  it('picks by hand from the places, no AMS unit among them, none selected until a click', async () => {
    const { user, onIdentified } = show()
    await user.click(screen.getByRole('radio', { name: 'Pick by hand' }))
    const list = await screen.findByRole('radiogroup', { name: 'Places' })
    await waitFor(() => expect(within(list).getAllByRole('radio').length).toBeGreaterThan(0))
    for (const row of within(list).getAllByRole('radio')) expect(row).toHaveAttribute('aria-checked', 'false')
    expect(within(list).queryByRole('radio', { name: /^L-6\b/ })).toBeNull()
    expect(within(list).getByRole('radio', { name: /^L-4\b/ })).toBeInTheDocument()
    await user.type(screen.getByRole('searchbox', { name: 'Find a place' }), 'dry')
    await waitFor(() => expect(within(list).getAllByRole('radio')).toHaveLength(1))
    expect(identified(onIdentified)).toHaveLength(0)
    await user.click(within(list).getByRole('radio', { name: /^L-3\b/ }))
    expect(identified(onIdentified)).toHaveLength(1)
    expect(identified(onIdentified)[0]).toMatchObject({ id: L.DRY_BOX })
  })

  it('names no place once the way changes', async () => {
    const { user, onIdentified } = show()
    await user.click(screen.getByRole('radio', { name: 'QR code' }))
    await user.type(screen.getByLabelText('Place code'), 'L-3{Enter}')
    await waitFor(() => expect(last(onIdentified)).toMatchObject({ id: L.DRY_BOX }))
    await user.click(screen.getByRole('radio', { name: 'Pick by hand' }))
    expect(last(onIdentified)).toBeNull()
  })

  it('offers no tag way and no camera in a frame', async () => {
    const camera = installCamera('https://sb.example/x/L-4')
    try {
      const { user } = show('embedded')
      expect(screen.queryByRole('radio', { name: 'Scan tag' })).toBeNull()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      expect(screen.getByLabelText('Place code')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Use camera' })).toBeNull()
    } finally {
      camera.restore()
    }
  })
})
