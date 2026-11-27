import { act, screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { seedEntityProperties } from '../../mocks/features/entities'
import { spoolId, T } from '../../mocks/features/filament/seed'
import { queueScanCodes } from '../../mocks/features/scan'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { installNdefStub, type NdefStub } from '../../test/ndefStub'
import { renderPage } from '../../test/utils'
import { IdentifySpool } from './IdentifySpool'

const OPENSPOOL_PLA = new TextEncoder().encode('{"protocol":"openspool","version":"1.0","type":"PLA","color_hex":"FFAABB","brand":"Generic","min_temp":"220","max_temp":"240"}')

function show() {
  const onIdentified = vi.fn()
  const view = renderPage(<IdentifySpool onIdentified={onIdentified} />)
  return { ...view, onIdentified }
}

/** The spools named so far, without the clears (`null`) between them. */
const identified = (fn: ReturnType<typeof vi.fn>) => fn.mock.calls.map((call) => call[0] as { id: string } | null).filter((one) => one !== null)

/** The last thing the caller was told: a spool, or null for "none named now". */
const last = (fn: ReturnType<typeof vi.fn>) => fn.mock.calls.at(-1)?.[0] as { id: string } | null | undefined

/** jsdom draws nothing: a canvas that "draws" and makes a small JPEG, and a photo it can open. */
function stubCanvas() {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: () => undefined } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function (callback: BlobCallback) {
    callback(new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], { type: 'image/jpeg' }))
  })
  vi.stubGlobal('createImageBitmap', async () => ({ width: 4000, height: 3000, close: () => undefined }))
}

/** The paths of every request, as they leave. */
function paths() {
  const sent: string[] = []
  const listen = ({ request }: { request: Request }) => void sent.push(`${request.method} ${new URL(request.url).pathname}`)
  server.events.on('request:start', listen)
  return { sent, off: () => server.events.removeListener('request:start', listen) }
}

/**
 * A stand-in for the Shape Detection API's BarcodeDetector, and a camera that gives a stream.
 * `supported` is what `getSupportedFormats` answers (no such method when it is missing), and
 * `formats` keeps the formats each detector was made with, and `breaks` makes the constructor throw.
 */
function installCamera(rawValue: string, { format = 'qr_code', supported, breaks = false }: { format?: string; supported?: string[]; breaks?: boolean } = {}) {
  const stop = vi.fn()
  const stream = { getTracks: () => [{ stop }] }
  const before = navigator.mediaDevices
  const formats: string[][] = []
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia: vi.fn(() => Promise.resolve(stream)) }, configurable: true })
  const Detector = class {
    constructor(options?: { formats: string[] }) {
      if (breaks) throw new Error('no detector')
      formats.push(options?.formats ?? [])
    }
    detect() {
      return Promise.resolve([{ rawValue, format }])
    }
  }
  if (supported) Object.assign(Detector, { getSupportedFormats: () => Promise.resolve(supported) })
  ;(window as unknown as { BarcodeDetector: unknown }).BarcodeDetector = Detector
  return {
    stop,
    formats,
    restore() {
      delete (window as unknown as { BarcodeDetector?: unknown }).BarcodeDetector
      Object.defineProperty(navigator, 'mediaDevices', { value: before, configurable: true })
    },
  }
}

describe('IdentifySpool (#7167)', () => {
  beforeEach(() => {
    resetMockState()
    stubCanvas()
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('offers the ways with none chosen, and no tag scan without Web NFC', () => {
    show()
    const ways = screen.getByRole('radiogroup', { name: 'How do you name the spool?' })
    expect(within(ways).queryByRole('radio', { name: 'Scan tag' })).toBeNull()
    for (const name of ['QR code', 'Pick by hand']) expect(within(ways).getByRole('radio', { name })).toHaveAttribute('aria-checked', 'false')
    expect(screen.queryByLabelText('Spool code')).toBeNull()
  })

  describe('with Web NFC', () => {
    let stub: NdefStub
    beforeEach(() => {
      stub = installNdefStub()
    })
    afterEach(() => stub.restore())

    it('identifies the spool a scanned tag is on', async () => {
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
      await screen.findByText("Hold the spool's tag to the phone.")
      await waitFor(() => expect(stub.scanning).toBe(true))
      stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
      await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
      expect(identified(onIdentified)[0]).toMatchObject({ id: spoolId(3), number: 3 })
    })

    it('makes a spool from a new tag, then identifies it', async () => {
      const log = paths()
      try {
        const { user, onIdentified } = show()
        await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
        await waitFor(() => expect(stub.scanning).toBe(true))
        stub.tap({ serialNumber: '04:77:66:55:44:33:22', records: [{ recordType: 'mime', mediaType: 'application/json', data: OPENSPOOL_PLA }] })
        expect(await screen.findByText('This tag is not on a spool yet.')).toBeInTheDocument()
        expect(identified(onIdentified)).toHaveLength(0)
        await user.click(screen.getByRole('button', { name: 'Pick it by hand' }))
        expect(await screen.findByRole('searchbox', { name: 'Find a spool' })).toBeInTheDocument()
        await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
        stub.tap({ serialNumber: '04:77:66:55:44:33:22', records: [{ recordType: 'mime', mediaType: 'application/json', data: OPENSPOOL_PLA }] })
        await user.click(await screen.findByRole('button', { name: 'Make a spool from this tag' }))
        await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
        const made = identified(onIdentified)[0] as { id: string }
        expect(log.sent).toContain('POST /api/v1/filament/spools')
        expect(log.sent.lastIndexOf('POST /api/v1/filament/tags/reads')).toBeGreaterThan(log.sent.indexOf('POST /api/v1/filament/spools'))
        expect(made.id).not.toBe(spoolId(3))
      } finally {
        log.off()
      }
    })
  })

  it('resolves a typed code, and says when it is not a spool', async () => {
    const { user, onIdentified } = show()
    await user.click(screen.getByRole('radio', { name: 'QR code' }))
    expect(screen.queryByRole('button', { name: 'Use camera' })).toBeNull()
    const field = screen.getByLabelText('Spool code')
    await user.type(field, 'L-1{Enter}')
    expect(await screen.findByText('L-1 is a place, not a spool.')).toBeInTheDocument()
    await user.clear(field)
    await user.type(field, 'S-999{Enter}')
    expect(await screen.findByText('No spool has the code S-999.')).toBeInTheDocument()
    await user.clear(field)
    await user.type(field, 's7')
    await user.click(screen.getByRole('button', { name: 'Find' }))
    await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
    expect(identified(onIdentified)[0]).toMatchObject({ id: spoolId(7) })
  })

  it('reads a label with the camera where BarcodeDetector exists', async () => {
    const camera = installCamera(`scadbuddy:spool:${spoolId(9)}`)
    try {
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      expect(screen.queryByRole('button', { name: 'Take a photo' })).toBeNull()
      await user.click(screen.getByRole('button', { name: 'Use camera' }))
      await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
      expect(identified(onIdentified)[0]).toMatchObject({ id: spoolId(9) })
      expect(camera.formats).toEqual([['qr_code', 'ean_13', 'ean_8', 'upc_a', 'upc_e']])
      await waitFor(() => expect(camera.stop).toHaveBeenCalled())
    } finally {
      camera.restore()
    }
  })

  it("reads a maker's EAN with the camera, in the formats the detector says it reads (#8372)", async () => {
    seedEntityProperties(`spool_type:${T.ORANGE}`, { gtin: ['6975373321234'] })
    const camera = installCamera('6975373321234', { format: 'ean_13', supported: ['qr_code', 'ean_13', 'code_128'] })
    try {
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      await user.click(screen.getByRole('button', { name: 'Use camera' }))
      expect(await screen.findByRole('searchbox', { name: 'Find a spool' })).toHaveValue('gtin:6975373321234')
      await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
      expect(identified(onIdentified)[0]).toMatchObject({ id: spoolId(2) })
      expect(camera.formats).toEqual([['qr_code', 'ean_13']])
      expect(camera.stop).toHaveBeenCalled()
    } finally {
      camera.restore()
    }
  })

  it('says so, once, when the camera detector cannot be made (#8372)', async () => {
    const camera = installCamera('x', { breaks: true })
    try {
      const { user } = show()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      await user.click(screen.getByRole('button', { name: 'Use camera' }))
      expect(await screen.findByText(/This browser cannot read codes from the camera/)).toBeInTheDocument()
      expect(screen.queryByLabelText('Camera preview')).toBeNull()
      expect(camera.stop).toHaveBeenCalled()
    } finally {
      camera.restore()
    }
  })

  it('reads a label from a photo where BarcodeDetector is missing', async () => {
    const log = paths()
    try {
      queueScanCodes([{ format: 'ean_13', text: '4006381333931' }, { format: 'qr_code', text: `scadbuddy:spool:${spoolId(5)}` }])
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      expect(screen.queryByRole('button', { name: 'Use camera' })).toBeNull()
      expect(screen.getByRole('button', { name: 'Take a photo' })).toBeInTheDocument()
      await user.upload(screen.getByLabelText('Photo of the label'), new File([new Uint8Array([0xff, 0xd8])], 'label.jpg', { type: 'image/jpeg' }))
      await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
      expect(identified(onIdentified)[0]).toMatchObject({ id: spoolId(5) })
      expect(log.sent.indexOf('POST /api/v1/scan/images')).toBeLessThan(log.sent.indexOf('POST /api/v1/scan/codes'))
    } finally {
      log.off()
    }
  })

  it('hides the photo button when the scan routes answer 404', async () => {
    server.use(http.post('/api/v1/scan/images', () => HttpResponse.json({ title: 'Not Found', status: 404, detail: 'not found' }, { status: 404 })))
    const { user, onIdentified } = show()
    await user.click(screen.getByRole('radio', { name: 'QR code' }))
    await user.upload(screen.getByLabelText('Photo of the label'), new File([new Uint8Array([0xff])], 'label.jpg', { type: 'image/jpeg' }))
    expect(await screen.findByText('This server reads no photos. Type the code from the label.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Take a photo' })).toBeNull()
    expect(screen.getByLabelText('Spool code')).toBeInTheDocument()
    expect(identified(onIdentified)).toHaveLength(0)
  })

  it('picks by hand from the unarchived spools, none selected until a click', async () => {
    const { user, onIdentified } = show()
    await user.click(screen.getByRole('radio', { name: 'Pick by hand' }))
    const list = await screen.findByRole('radiogroup', { name: 'Spools' })
    await waitFor(() => expect(within(list).getAllByRole('radio').length).toBeGreaterThan(0))
    for (const row of within(list).getAllByRole('radio')) expect(row).toHaveAttribute('aria-checked', 'false')
    expect(within(list).queryByRole('radio', { name: /S-19\b/ })).toBeNull()
    await user.type(screen.getByRole('searchbox', { name: 'Find a spool' }), 'teal')
    await waitFor(() => expect(within(list).getAllByRole('radio')).toHaveLength(1))
    expect(identified(onIdentified)).toHaveLength(0)
    await user.click(within(list).getByRole('radio', { name: /S-6\b/ }))
    expect(identified(onIdentified)).toHaveLength(1)
    expect(identified(onIdentified)[0]).toMatchObject({ id: spoolId(6) })
  })

  it('clears the named spool when the code changes, a lookup fails or the way changes (review I1)', async () => {
    const { user, onIdentified } = show()
    await user.click(screen.getByRole('radio', { name: 'QR code' }))
    const field = screen.getByLabelText('Spool code')
    await user.type(field, 'S-7{Enter}')
    await waitFor(() => expect(last(onIdentified)).toMatchObject({ id: spoolId(7) }))
    // Editing the code: the earlier spool is no longer named.
    await user.type(field, '7')
    expect(last(onIdentified)).toBeNull()
    await user.type(field, '{Enter}')
    expect(await screen.findByText('No spool has the code S-77.')).toBeInTheDocument()
    expect(last(onIdentified)).toBeNull()
    await user.clear(field)
    await user.type(field, 'S-7{Enter}')
    await waitFor(() => expect(last(onIdentified)).toMatchObject({ id: spoolId(7) }))
    await user.click(screen.getByRole('radio', { name: 'Pick by hand' }))
    expect(last(onIdentified)).toBeNull()
  })

  it('names nothing for a typed lookup that is still out, and says no photo is read (review M2)', async () => {
    let release = () => {}
    const held = new Promise<void>((resolve) => (release = resolve))
    server.use(
      http.get('/api/v1/codes/:code', async () => {
        await held
        return HttpResponse.json({ title: 'Not Found', status: 404, detail: 'no', code: 'code_unknown' }, { status: 404 })
      }),
    )
    const { user } = show()
    await user.click(screen.getByRole('radio', { name: 'QR code' }))
    await user.type(screen.getByLabelText('Spool code'), 'S-5{Enter}')
    expect(screen.queryByText('Reading the photo…')).toBeNull()
    expect(screen.getByRole('button', { name: 'Take a photo' })).toBeInTheDocument()
    release()
    expect(await screen.findByText('No spool has the code S-5.')).toBeInTheDocument()
  })

  it('sends a photo drawn again as a JPEG, whatever its type (review M1)', async () => {
    const types: (string | null)[] = []
    const listen = ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname === '/api/v1/scan/images') types.push(request.headers.get('Content-Type'))
    }
    server.events.on('request:start', listen)
    try {
      queueScanCodes([{ format: 'qr_code', text: `scadbuddy:spool:${spoolId(5)}` }])
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      await user.upload(screen.getByLabelText('Photo of the label'), new File([new Uint8Array([0, 0, 0, 24])], 'IMG_0001.HEIC', { type: 'image/heic' }))
      await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
      expect(types).toEqual(['image/jpeg'])
    } finally {
      server.events.removeListener('request:start', listen)
    }
  })

  describe('with Web NFC, the guards (review I2, M3)', () => {
    let stub: NdefStub
    beforeEach(() => {
      stub = installNdefStub()
    })
    afterEach(() => stub.restore())

    it('refuses a tag on an archived spool', async () => {
      server.use(http.post('/api/v1/filament/tags/reads', () => HttpResponse.json({ next: 'open_spool', spool: { id: spoolId(19), number: 19, archived_at: '2026-11-01T00:00:00Z' }, tag: null, decoded: null, draft: null })))
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
      await waitFor(() => expect(stub.scanning).toBe(true))
      stub.tap({ serialNumber: '04:19:19:19:19:19:19', records: [] })
      expect(await screen.findByText('S-19 is archived.')).toBeInTheDocument()
      expect(identified(onIdentified)).toHaveLength(0)
    })

    it('names the spool of the newest tap when an older read answers last (review 6)', async () => {
      let release!: () => void
      const held = new Promise<void>((resolve) => (release = resolve))
      server.use(
        http.post('/api/v1/filament/tags/reads', async ({ request }) => {
          const body = (await request.clone().json()) as { uid: string }
          const number = body.uid === '04:05:05:05:05:05:05' ? 5 : 3
          if (number === 3) await held
          return HttpResponse.json({ next: 'open_spool', spool: { id: spoolId(number), number, archived_at: null }, tag: null, decoded: null, draft: null })
        }),
      )
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
      await waitFor(() => expect(stub.scanning).toBe(true))
      stub.tap({ serialNumber: '04:03:03:03:03:03:03', records: [] })
      stub.tap({ serialNumber: '04:05:05:05:05:05:05', records: [] })
      await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
      release()
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(identified(onIdentified)).toEqual([expect.objectContaining({ id: spoolId(5) })])
      expect(last(onIdentified)).toMatchObject({ id: spoolId(5) })
    })

    it('keeps the spool a failed bind made, and binds it on the retry without a second one', async () => {
      const log = paths()
      let binds = 0
      server.use(
        http.post('/api/v1/filament/tags/reads', async ({ request }) => {
          const body = (await request.clone().json()) as { bind_spool_id?: string }
          if (body.bind_spool_id && (binds += 1) === 1) return HttpResponse.json({ title: 'Error', status: 500, detail: 'the bind failed here' }, { status: 500 })
          return undefined
        }),
      )
      try {
        const { user, onIdentified } = show()
        await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
        await waitFor(() => expect(stub.scanning).toBe(true))
        stub.tap({ serialNumber: '04:77:66:55:44:33:23', records: [{ recordType: 'mime', mediaType: 'application/json', data: OPENSPOOL_PLA }] })
        await user.click(await screen.findByRole('button', { name: 'Make a spool from this tag' }))
        const retry = await screen.findByRole('button', { name: /^Bind to spool #\d+$/ })
        expect(identified(onIdentified)).toHaveLength(0)
        await user.click(retry)
        await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
        expect(log.sent.filter((line) => line === 'POST /api/v1/filament/spools')).toHaveLength(1)
        expect(binds).toBe(2)
      } finally {
        log.off()
      }
    })
  })

  describe('with Web NFC, a Bambu tag (#7168)', () => {
    let stub: NdefStub
    let bodies: Record<string, unknown>[]
    const listen = async ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname === '/api/v1/filament/tags/reads') bodies.push((await request.clone().json()) as Record<string, unknown>)
    }
    beforeEach(() => {
      stub = installNdefStub()
      bodies = []
      server.events.on('request:start', listen)
    })
    afterEach(() => {
      server.events.removeListener('request:start', listen)
      delete window.ScadBuddyNfc
      stub.restore()
    })

    it("posts the app's MIFARE blocks for the tapped tag", async () => {
      window.ScadBuddyNfc = { native: true, lastMifare: { uid: '1A2B3C4D', blocks: [{ index: 1, dataHex: '4131372d503100004746413137000000' }] } }
      const { user } = show()
      await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
      await waitFor(() => expect(stub.scanning).toBe(true))
      stub.tap({ serialNumber: '1a:2b:3c:4d', records: [] })
      await waitFor(() => expect(bodies).toHaveLength(1))
      expect(bodies[0]).toEqual({ uid: '1A2B3C4D', tag_type: 'mifare_classic', mifare_blocks: [{ index: 1, data_hex: '4131372d503100004746413137000000' }], records: [] })
    })

    it('posts no blocks without the app, and offers the pick by hand for an unknown side', async () => {
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
      await waitFor(() => expect(stub.scanning).toBe(true))
      stub.tap({ serialNumber: '1a:2b:3c:4d', records: [] })
      expect(await screen.findByText("This side of the Bambu tag is on no spool yet. Tap the other side, use the QR code, or pick the spool by hand.")).toBeInTheDocument()
      expect(bodies).toHaveLength(1)
      expect(bodies[0]).not.toHaveProperty('mifare_blocks')
      expect(screen.queryByText('This tag is not on a spool yet.')).toBeNull()
      expect(identified(onIdentified)).toHaveLength(0)
      await user.click(screen.getByRole('button', { name: 'Pick it by hand' }))
      expect(await screen.findByRole('searchbox', { name: 'Find a spool' })).toBeInTheDocument()
      expect(screen.getByRole('radio', { name: 'Pick by hand' })).toHaveAttribute('aria-checked', 'true')
    })

    it('posts the read again without blocks when the server cannot decode them', async () => {
      window.ScadBuddyNfc = { native: true, lastMifare: { uid: '04A1B2C3D4E5F6', blocks: [{ index: 1, dataHex: '4131372d503100004746413137000000' }] } }
      server.use(
        http.post('/api/v1/filament/tags/reads', async ({ request }) => {
          const body = (await request.clone().json()) as { mifare_blocks?: unknown }
          if (body.mifare_blocks) return HttpResponse.json({ title: 'Service Unavailable', status: 503, detail: 'core is off', code: 'mifare_needs_core' }, { status: 503 })
          return undefined
        }),
      )
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
      await waitFor(() => expect(stub.scanning).toBe(true))
      stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
      await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
      expect(identified(onIdentified)[0]).toMatchObject({ id: spoolId(3) })
      expect(bodies).toHaveLength(2)
      expect(bodies[1]).not.toHaveProperty('mifare_blocks')
    })
  })

  describe('with Web NFC, a photo read that answers after the way changed (#8372)', () => {
    let stub: NdefStub
    let release!: () => void
    let reached!: Promise<void>
    let answered!: Promise<void>
    const off: (() => void)[] = []
    beforeEach(() => {
      stub = installNdefStub()
      const held = new Promise<void>((resolve) => (release = resolve))
      let reach!: () => void
      reached = new Promise<void>((resolve) => (reach = resolve))
      server.use(
        http.post('/api/v1/scan/codes', async () => {
          reach()
          await held
          return undefined
        }),
      )
      let answer!: () => void
      answered = new Promise<void>((resolve) => (answer = resolve))
      const listen = ({ request }: { request: Request }) => {
        if (new URL(request.url).pathname === '/api/v1/scan/codes') answer()
      }
      server.events.on('response:mocked', listen)
      off.push(() => server.events.removeListener('response:mocked', listen))
    })
    afterEach(() => {
      off.splice(0).forEach((stop) => stop())
      stub.restore()
    })

    /** Uploads a photo in the QR code way, and waits until its read is held at the server. */
    async function holdPhoto(user: ReturnType<typeof show>['user']) {
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      await user.upload(screen.getByLabelText('Photo of the label'), new File([new Uint8Array([0xff, 0xd8])], 'label.jpg', { type: 'image/jpeg' }))
      await reached
    }

    it('starts no search and moves no way for a late barcode', async () => {
      queueScanCodes([{ format: 'ean_13', text: '6975373321234' }])
      const { user, onIdentified } = show()
      await holdPhoto(user)
      await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
      const calls = onIdentified.mock.calls.length
      release()
      await answered
      await act(async () => {})
      expect(screen.getByRole('radio', { name: 'Scan tag' })).toHaveAttribute('aria-checked', 'true')
      expect(screen.queryByRole('searchbox', { name: 'Find a spool' })).toBeNull()
      expect(onIdentified.mock.calls.length).toBe(calls)
    })

    it("keeps the spool a tag named when the photo's lookup answers late", async () => {
      queueScanCodes([{ format: 'qr_code', text: `scadbuddy:spool:${spoolId(5)}` }])
      let fetched!: () => void
      const spoolRead = new Promise<void>((resolve) => (fetched = resolve))
      const listen = ({ request }: { request: Request }) => {
        if (new URL(request.url).pathname === `/api/v1/filament/spools/${spoolId(5)}`) fetched()
      }
      server.events.on('response:mocked', listen)
      off.push(() => server.events.removeListener('response:mocked', listen))
      const { user, onIdentified } = show()
      await holdPhoto(user)
      await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
      await waitFor(() => expect(stub.scanning).toBe(true))
      stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
      await waitFor(() => expect(last(onIdentified)).toMatchObject({ id: spoolId(3) }))
      const calls = onIdentified.mock.calls.length
      release()
      await answered
      await spoolRead
      await act(async () => {})
      expect(onIdentified.mock.calls.length).toBe(calls)
      expect(last(onIdentified)).toMatchObject({ id: spoolId(3) })
    })
  })

  it('offers no tag way and no camera in a frame', async () => {
    const camera = installCamera(`scadbuddy:spool:${spoolId(9)}`)
    try {
      const onIdentified = vi.fn()
      const { user } = renderPage(<IdentifySpool onIdentified={onIdentified} nfc="embedded" />)
      expect(screen.queryByRole('radio', { name: 'Scan tag' })).toBeNull()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      expect(screen.getByLabelText('Spool code')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Use camera' })).toBeNull()
      expect(screen.getByRole('button', { name: 'Take a photo' })).toBeInTheDocument()
    } finally {
      camera.restore()
    }
  })

  describe('a code that names no ScadBuddy thing searches the spools (#8372)', () => {
    const GTIN = '6975373321234'

    /** Types the code in the QR code way, and waits for the switch to "Pick by hand" with the search in the box. */
    async function scan(user: ReturnType<typeof show>['user']) {
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      await user.type(screen.getByLabelText('Spool code'), `${GTIN}{Enter}`)
      expect(await screen.findByRole('searchbox', { name: 'Find a spool' })).toHaveValue(`gtin:${GTIN}`)
      expect(screen.getByRole('radio', { name: 'Pick by hand' })).toHaveAttribute('aria-checked', 'true')
      return screen.findByRole('radiogroup', { name: 'Spools' })
    }

    it('names the one spool whose type has the code at once', async () => {
      seedEntityProperties(`spool_type:${T.ORANGE}`, { gtin: [GTIN] })
      const { user, onIdentified } = show()
      const list = await scan(user)
      await waitFor(() => expect(identified(onIdentified)).toHaveLength(1))
      expect(identified(onIdentified)[0]).toMatchObject({ id: spoolId(2) })
      expect(within(list).getByRole('radio', { name: /S-2\b/ })).toHaveAttribute('aria-checked', 'true')
    })

    it('names the one spool once under StrictMode, which runs the effects twice', async () => {
      seedEntityProperties(`spool_type:${T.ORANGE}`, { gtin: [GTIN] })
      const onIdentified = vi.fn()
      const { user } = renderPage(
        <StrictMode>
          <IdentifySpool onIdentified={onIdentified} />
        </StrictMode>,
      )
      const list = await scan(user)
      await waitFor(() => expect(within(list).getByRole('radio', { name: /S-2\b/ })).toHaveAttribute('aria-checked', 'true'))
      expect(identified(onIdentified)).toEqual([expect.objectContaining({ id: spoolId(2) })])
    })

    it('shows several matches and names none, and an edit of the box later names none', async () => {
      seedEntityProperties(`spool_type:${T.ORANGE}`, { gtin: [GTIN] })
      seedEntityProperties(`spool_type:${T.PINK}`, { gtin: [GTIN] })
      const { user, onIdentified } = show()
      const list = await scan(user)
      await waitFor(() => expect(within(list).getAllByRole('radio')).toHaveLength(2))
      expect(within(list).getByRole('radio', { name: /S-2\b/ })).toBeInTheDocument()
      expect(within(list).getByRole('radio', { name: /S-3\b/ })).toBeInTheDocument()
      expect(identified(onIdentified)).toHaveLength(0)
      await user.type(screen.getByRole('searchbox', { name: 'Find a spool' }), ' pink')
      await waitFor(() => expect(within(list).getAllByRole('radio')).toHaveLength(1))
      expect(within(list).getByRole('radio', { name: /S-3\b/ })).toHaveAttribute('aria-checked', 'false')
      expect(identified(onIdentified)).toHaveLength(0)
    })

    it('shows an empty list with the code in the box when no spool has it', async () => {
      const { user, onIdentified } = show()
      const list = await scan(user)
      expect(await screen.findByText('No spool matches. Check the code on its label.')).toBeInTheDocument()
      expect(within(list).queryAllByRole('radio')).toHaveLength(0)
      expect(screen.getByRole('searchbox', { name: 'Find a spool' })).toHaveValue(`gtin:${GTIN}`)
      expect(identified(onIdentified)).toHaveLength(0)
    })

    it('searches the code a photo reads when it names no ScadBuddy thing', async () => {
      queueScanCodes([{ format: 'ean_13', text: GTIN }])
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      await user.upload(screen.getByLabelText('Photo of the label'), new File([new Uint8Array([0xff, 0xd8])], 'label.jpg', { type: 'image/jpeg' }))
      expect(await screen.findByRole('searchbox', { name: 'Find a spool' })).toHaveValue(`gtin:${GTIN}`)
      expect(screen.getByRole('radio', { name: 'Pick by hand' })).toHaveAttribute('aria-checked', 'true')
      expect(identified(onIdentified)).toHaveLength(0)
    })

    it('keeps the sentence for a photo of an unknown ScadBuddy code (D11)', async () => {
      queueScanCodes([{ format: 'qr_code', text: 'S-999' }])
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      await user.upload(screen.getByLabelText('Photo of the label'), new File([new Uint8Array([0xff, 0xd8])], 'label.jpg', { type: 'image/jpeg' }))
      expect(await screen.findByText('No spool label was found in the photo. Take it again, or type the code.')).toBeInTheDocument()
      expect(screen.queryByRole('searchbox', { name: 'Find a spool' })).toBeNull()
      expect(screen.getByRole('radio', { name: 'QR code' })).toHaveAttribute('aria-checked', 'true')
      expect(identified(onIdentified)).toHaveLength(0)
    })

    it('names no spool when the box is edited before the property reads settle', async () => {
      seedEntityProperties(`spool_type:${T.ORANGE}`, { gtin: [GTIN] })
      let release!: () => void
      const held = new Promise<void>((resolve) => (release = resolve))
      server.use(
        http.get('/api/v1/entity-kinds/spool_type/properties', async () => {
          await held
          return undefined
        }),
      )
      let handled!: () => void
      const answered = new Promise<void>((resolve) => (handled = resolve))
      const listen = ({ request }: { request: Request }) => {
        if (new URL(request.url).pathname === '/api/v1/entity-kinds/spool_type/properties') handled()
      }
      server.events.on('response:mocked', listen)
      try {
        const { user, onIdentified } = show()
        await scan(user)
        expect(screen.getByText(`Looking for “gtin:${GTIN}”…`)).toBeInTheDocument()
        // A space keeps the one match, so only the edit can stop the pick.
        await user.type(screen.getByRole('searchbox', { name: 'Find a spool' }), ' ')
        release()
        await answered
        await act(async () => {})
        const list = screen.getByRole('radiogroup', { name: 'Spools' })
        expect(await within(list).findByRole('radio', { name: /S-2\b/ })).toHaveAttribute('aria-checked', 'false')
        expect(within(list).getAllByRole('radio')).toHaveLength(1)
        expect(identified(onIdentified)).toHaveLength(0)
      } finally {
        server.events.removeListener('response:mocked', listen)
      }
    })

    it('finds a spool by its code and a property of its type in one search', async () => {
      seedEntityProperties(`spool_type:${T.ORANGE}`, { gtin: [GTIN] })
      seedEntityProperties(`spool_type:${T.PINK}`, { gtin: [GTIN] })
      const { user } = show()
      await user.click(screen.getByRole('radio', { name: 'Pick by hand' }))
      const list = await screen.findByRole('radiogroup', { name: 'Spools' })
      await waitFor(() => expect(within(list).getAllByRole('radio').length).toBeGreaterThan(1))
      await user.type(screen.getByRole('searchbox', { name: 'Find a spool' }), `S-2 gtin:${GTIN}`)
      await waitFor(() => expect(within(list).getAllByRole('radio')).toHaveLength(1))
      expect(within(list).getByRole('radio', { name: /S-2\b/ })).toBeInTheDocument()
    })

    it('keeps the sentence for a label URL whose code is not found (D11)', async () => {
      const { user } = show()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      await user.type(screen.getByLabelText('Spool code'), 'https://h/x/L-99{Enter}')
      expect(await screen.findByText('No spool has the code L-99.')).toBeInTheDocument()
      expect(screen.queryByRole('searchbox', { name: 'Find a spool' })).toBeNull()
    })

    it('keeps the sentence for a photo of a spool label whose spool is gone (D11)', async () => {
      queueScanCodes([{ format: 'qr_code', text: 'scadbuddy:spool:0b6c1f2e-1d2a-4c3b-9e8f-7a6b5c4d3e2f' }])
      const { user } = show()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      await user.upload(screen.getByLabelText('Photo of the label'), new File([new Uint8Array([0xff, 0xd8])], 'label.jpg', { type: 'image/jpeg' }))
      expect(await screen.findByText('No spool label was found in the photo. Take it again, or type the code.')).toBeInTheDocument()
      expect(screen.queryByRole('searchbox', { name: 'Find a spool' })).toBeNull()
    })

    it('keeps the sentence for a ScadBuddy code that is not found', async () => {
      const { user } = show()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      await user.type(screen.getByLabelText('Spool code'), 'S-999{Enter}')
      expect(await screen.findByText('No spool has the code S-999.')).toBeInTheDocument()
      expect(screen.queryByRole('searchbox', { name: 'Find a spool' })).toBeNull()
    })
  })
})
