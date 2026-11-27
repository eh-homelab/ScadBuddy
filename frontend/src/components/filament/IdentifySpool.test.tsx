import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { spoolId } from '../../mocks/features/filament/seed'
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

/** The paths of every request, as they leave. */
function paths() {
  const sent: string[] = []
  const listen = ({ request }: { request: Request }) => void sent.push(`${request.method} ${new URL(request.url).pathname}`)
  server.events.on('request:start', listen)
  return { sent, off: () => server.events.removeListener('request:start', listen) }
}

/** A stand-in for the Shape Detection API's BarcodeDetector, and a camera that gives a stream. */
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

describe('IdentifySpool (#7167)', () => {
  beforeEach(() => resetMockState())

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
      await waitFor(() => expect(onIdentified).toHaveBeenCalledTimes(1))
      expect(onIdentified.mock.calls[0]![0]).toMatchObject({ id: spoolId(3), number: 3 })
    })

    it('makes a spool from a new tag, then identifies it', async () => {
      const log = paths()
      try {
        const { user, onIdentified } = show()
        await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
        await waitFor(() => expect(stub.scanning).toBe(true))
        stub.tap({ serialNumber: '04:77:66:55:44:33:22', records: [{ recordType: 'mime', mediaType: 'application/json', data: OPENSPOOL_PLA }] })
        expect(await screen.findByText('This tag is not on a spool yet.')).toBeInTheDocument()
        expect(onIdentified).not.toHaveBeenCalled()
        await user.click(screen.getByRole('button', { name: 'Pick it by hand' }))
        expect(await screen.findByRole('searchbox', { name: 'Find a spool' })).toBeInTheDocument()
        await user.click(screen.getByRole('radio', { name: 'Scan tag' }))
        stub.tap({ serialNumber: '04:77:66:55:44:33:22', records: [{ recordType: 'mime', mediaType: 'application/json', data: OPENSPOOL_PLA }] })
        await user.click(await screen.findByRole('button', { name: 'Make a spool from this tag' }))
        await waitFor(() => expect(onIdentified).toHaveBeenCalledTimes(1))
        const made = onIdentified.mock.calls[0]![0] as { id: string }
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
    await waitFor(() => expect(onIdentified).toHaveBeenCalledTimes(1))
    expect(onIdentified.mock.calls[0]![0]).toMatchObject({ id: spoolId(7) })
  })

  it('reads a label with the camera where BarcodeDetector exists', async () => {
    const camera = installCamera(`scadbuddy:spool:${spoolId(9)}`)
    try {
      const { user, onIdentified } = show()
      await user.click(screen.getByRole('radio', { name: 'QR code' }))
      expect(screen.queryByRole('button', { name: 'Take a photo' })).toBeNull()
      await user.click(screen.getByRole('button', { name: 'Use camera' }))
      await waitFor(() => expect(onIdentified).toHaveBeenCalledTimes(1))
      expect(onIdentified.mock.calls[0]![0]).toMatchObject({ id: spoolId(9) })
      await waitFor(() => expect(camera.stop).toHaveBeenCalled())
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
      await waitFor(() => expect(onIdentified).toHaveBeenCalledTimes(1))
      expect(onIdentified.mock.calls[0]![0]).toMatchObject({ id: spoolId(5) })
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
    expect(onIdentified).not.toHaveBeenCalled()
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
    expect(onIdentified).not.toHaveBeenCalled()
    await user.click(within(list).getByRole('radio', { name: /S-6\b/ }))
    expect(onIdentified).toHaveBeenCalledTimes(1)
    expect(onIdentified.mock.calls[0]![0]).toMatchObject({ id: spoolId(6) })
  })
})
