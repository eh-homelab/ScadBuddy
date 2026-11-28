import { screen, waitFor, within } from '@testing-library/react'
import { useEffect } from 'react'
import { useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { L, spoolId } from '../../../mocks/features/filament/seed'
import { queueScanCodes } from '../../../mocks/features/scan'
import { server } from '../../../mocks/server'
import { filamentLinks } from '../../../lib/filamentLinks'
import { scanSearchPath } from '../../../lib/scanSearch'
import { renderPage } from '../../../test/utils'
import { ScanCameraProvider } from './ScanCameraProvider'
import { ScanCode } from './ScanCode'
import { useScanCamera } from './scanCamera'

function Here() {
  const location = useLocation()
  return <output aria-label="Current address">{location.pathname + location.search}</output>
}

const open = () =>
  renderPage(
    <ScanCameraProvider>
      <ScanCode />
      <Here />
    </ScanCameraProvider>,
  )

/** Each scan request's path and JSON body. */
function sent() {
  const requests: { line: string; body: unknown }[] = []
  const listener = ({ request }: { request: Request }) => {
    const url = new URL(request.url)
    if (!url.pathname.startsWith('/api/v1/scan/')) return
    const type = request.headers.get('Content-Type') ?? ''
    void request
      .clone()
      .text()
      .then((text) => requests.push({ line: `${request.method} ${url.pathname}`, body: type.includes('json') && text ? JSON.parse(text) : type }))
  }
  server.events.on('request:start', listener)
  return { requests, off: () => server.events.removeListener('request:start', listener) }
}

let stopped = 0
const stream = { getTracks: () => [{ stop: () => (stopped += 1) }] } as unknown as MediaStream

beforeEach(() => {
  stopped = 0
  // jsdom draws nothing: a canvas that "draws" and makes a small JPEG.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: () => undefined } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function (callback: BlobCallback) {
    callback(new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], { type: 'image/jpeg' }))
  })
  vi.stubGlobal('createImageBitmap', async () => ({ width: 4000, height: 3000, close: () => undefined }))
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
  Object.defineProperty(HTMLVideoElement.prototype, 'videoWidth', { configurable: true, get: () => 1280 })
  Object.defineProperty(HTMLVideoElement.prototype, 'videoHeight', { configurable: true, get: () => 720 })
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  Reflect.deleteProperty(navigator, 'mediaDevices')
  Reflect.deleteProperty(navigator, 'geolocation')
})

function giveCamera(getUserMedia: () => Promise<MediaStream>) {
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn(getUserMedia) } })
}

describe('ScanCode (#8155, a code scanned from a photo)', () => {
  it('scans a still of the live preview and opens the spool its label names', async () => {
    giveCamera(async () => stream)
    Object.defineProperty(navigator, 'geolocation', {
      configurable: true,
      value: { getCurrentPosition: (ok: PositionCallback) => ok({ coords: { latitude: 43.65, longitude: -79.38, accuracy: 9 }, timestamp: 0 } as GeolocationPosition) },
    })
    queueScanCodes([{ format: 'qr_code', text: `scadbuddy:spool:${spoolId(1)}` }])
    const log = sent()
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Start camera' }))
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledWith(expect.objectContaining({ video: expect.objectContaining({ facingMode: { ideal: 'environment' } }) }))
    expect(await screen.findByLabelText('Camera preview')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Scan code' }))

    const card = await screen.findByRole('region', { name: 'Spool S-1' })
    expect(within(card).getByRole('link', { name: 'Open' })).toHaveAttribute('href', filamentLinks.spool(spoolId(1)))
    expect(screen.getByText(/Taken at 43\.65000, -79\.38000 \(within 9 m\)/)).toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'Codes scanned' })).getByText('Spool S-1')).toBeInTheDocument()
    // The photo is the JPEG's own bytes, then a JSON call that names its key: no image inside JSON.
    await waitFor(() => expect(log.requests.map((one) => one.line)).toEqual(['POST /api/v1/scan/images', 'POST /api/v1/scan/codes']))
    expect(log.requests[0]!.body).toBe('image/jpeg')
    expect(log.requests[1]!.body).toMatchObject({ source: 'shutter', key: expect.stringMatching(/^scan_image-[0-9a-f]{64}$/), location: { lat: 43.65, lon: -79.38, accuracy_m: 9 } })
    log.off()

    await user.click(screen.getByRole('button', { name: 'Stop camera' }))
    expect(stopped).toBe(1)
    expect(screen.getByRole('button', { name: 'Start camera' })).toBeInTheDocument()
  })

  it('offers a chosen photo when the browser gives no camera, and shows other text with its format', async () => {
    queueScanCodes([
      { format: 'ean_13', text: '4006381333931' },
      { format: 'qr_code', text: 'https://example.com/datasheet' },
      { format: 'qr_code', text: 'https://scadbuddy.example/x/L-4' },
    ])
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Start camera' }))
    expect(screen.getByText(/This browser gives the page no camera/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start camera' })).toBeNull()

    const log = sent()
    await user.upload(screen.getByLabelText('Photo of a code'), new File(['heic bytes'], 'IMG_0001.HEIC', { type: 'image/heic' }))
    const codes = await screen.findByRole('list', { name: 'Codes in this photo' })
    expect(within(codes).getByText('EAN-13')).toBeInTheDocument()
    expect(within(codes).getByText('4006381333931')).toBeInTheDocument()
    expect(within(codes).getByRole('link', { name: 'Open this address' })).toHaveAttribute('href', 'https://example.com/datasheet')
    expect(within(codes).getByRole('region', { name: 'Place L-4' })).toBeInTheDocument()
    expect(within(codes).getByRole('link', { name: 'Open' })).toHaveAttribute('href', filamentLinks.location(L.AMS_A1))
    expect(screen.getByText('EAN-13 and 2 more')).toBeInTheDocument()
    // The photo was drawn again as a JPEG, so a HEIC the browser opens is sent as one.
    await waitFor(() => expect(log.requests[0]).toEqual({ line: 'POST /api/v1/scan/images', body: 'image/jpeg' }))
    await waitFor(() => expect(log.requests[1]?.body).toMatchObject({ source: 'file', location: null }))
    log.off()
  })

  it('links an unknown code to the spool search, and goes there only on a tap (#8372)', async () => {
    queueScanCodes([
      { format: 'ean_13', text: '6975373321234' },
      { format: 'qr_code', text: 'https://example.com/datasheet' },
      { format: 'qr_code', text: 'https://scadbuddy.example/x/L-4' },
    ])
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Start camera' }))
    await user.upload(screen.getByLabelText('Photo of a code'), new File(['bytes'], 'a.jpg', { type: 'image/jpeg' }))
    const codes = await screen.findByRole('list', { name: 'Codes in this photo' })
    const ean = within(codes).getByRole('link', { name: 'Search spools for 6975373321234' })
    expect(ean).toHaveAttribute('href', scanSearchPath('6975373321234'))
    expect(within(codes).getByRole('link', { name: 'Search spools for https://example.com/datasheet' })).toHaveAttribute('href', scanSearchPath('https://example.com/datasheet'))
    expect(within(codes).getByRole('link', { name: 'Open this address' })).toBeInTheDocument()
    // The resolved place has no search link.
    expect(within(codes).getAllByRole('link', { name: /^Search spools for/ })).toHaveLength(2)
    expect(screen.getByLabelText('Current address')).toHaveTextContent(/^\/$/)
    await user.click(screen.getByRole('button', { name: 'Read again' }))
    await within(await screen.findByRole('list', { name: 'Codes in this photo' })).findByRole('link', { name: 'Search spools for 6975373321234' })
    expect(screen.getByLabelText('Current address')).toHaveTextContent(/^\/$/)
    await user.click(ean)
    expect(screen.getByLabelText('Current address')).toHaveTextContent(scanSearchPath('6975373321234'))
  })

  it('gives an unknown ScadBuddy code, label URL or label text no search link (#8372)', async () => {
    const gone = 'scadbuddy:spool:0b6c1f2e-1d2a-4c3b-9e8f-7a6b5c4d3e2f'
    queueScanCodes([
      { format: 'qr_code', text: 'S-999' },
      { format: 'qr_code', text: 'https://scadbuddy.example/x/L-99' },
      { format: 'qr_code', text: gone },
    ])
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Start camera' }))
    await user.upload(screen.getByLabelText('Photo of a code'), new File(['bytes'], 'a.jpg', { type: 'image/jpeg' }))
    const codes = await screen.findByRole('list', { name: 'Codes in this photo' })
    expect(within(codes).getByText('S-999')).toBeInTheDocument()
    expect(within(codes).getByText('https://scadbuddy.example/x/L-99')).toBeInTheDocument()
    expect(within(codes).getByText(gone)).toBeInTheDocument()
    expect(within(codes).queryByRole('link', { name: /^Search spools for/ })).toBeNull()
  })

  it('stops a camera that opens after Stop, or after the page is gone', async () => {
    let give: (value: MediaStream) => void = () => undefined
    giveCamera(() => new Promise<MediaStream>((resolve) => (give = resolve)))
    const { user, unmount } = open()
    await user.click(screen.getByRole('button', { name: 'Start camera' }))
    await user.click(screen.getByRole('button', { name: 'Stop camera' }))
    give(stream)
    await waitFor(() => expect(stopped).toBe(1))
    expect(screen.queryByLabelText('Camera preview')).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Start camera' }))
    unmount()
    give(stream)
    await waitFor(() => expect(stopped).toBe(2))
  })

  it('says why the camera is refused', async () => {
    giveCamera(async () => {
      throw new DOMException('denied', 'NotAllowedError')
    })
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Start camera' }))
    expect(await screen.findByText(/The camera is not allowed for this page/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Choose a photo' })).toBeInTheDocument()
  })

  it('keeps a scan that read nothing, reads it again, and deletes it', async () => {
    const { user } = open()
    await user.upload(screen.getByLabelText('Photo of a code'), new File(['x'], 'shelf.jpg', { type: 'image/jpeg' }))
    expect(await screen.findByText(/No code found\. Hold the code flat/)).toBeInTheDocument()
    expect(screen.getByRole('img', { name: /The photo scanned at/ })).toHaveAttribute('src', expect.stringMatching(/^\/api\/v1\/scan\/scans\/.+\/image$/))

    queueScanCodes([{ format: 'code_128', text: 'LOT-2026-11' }])
    await user.click(screen.getByRole('button', { name: 'Read again' }))
    expect(await screen.findByText('LOT-2026-11')).toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'Codes in this photo' })).getByText('Code 128')).toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'Codes scanned' })).getByText('Code 128')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Delete scan' }))
    await waitFor(() => expect(screen.queryByRole('list', { name: 'Codes scanned' })).toBeNull())
  })

  it('gives another read the photo of the preview (an NFC read keeps its own)', async () => {
    giveCamera(async () => stream)
    const take: { current: (() => Promise<unknown>) | null } = { current: null }
    function NfcRead() {
      const camera = useScanCamera()
      useEffect(() => {
        take.current = () => camera!.capture('nfc')
      }, [camera])
      return null
    }
    const { user } = renderPage(
      <ScanCameraProvider>
        <ScanCode />
        <NfcRead />
      </ScanCameraProvider>,
    )
    await user.click(screen.getByRole('button', { name: 'Start camera' }))
    await screen.findByLabelText('Camera preview')
    const taken = (await take.current!()) as { key: string; mediaType: string; scan: { source: string } }
    expect(taken.key).toMatch(/^scan_image-/)
    expect(taken.mediaType).toBe('image/jpeg')
    expect(taken.scan.source).toBe('nfc')
  })
})
