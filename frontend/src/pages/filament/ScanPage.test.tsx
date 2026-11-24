import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { listTags } from '../../api/filament'
import { spoolId } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { filamentLinks } from '../../lib/filamentLinks'
import { installNdefStub, type NdefStub } from '../../test/ndefStub'
import { renderPage } from '../../test/utils'
import { ScanPage } from './ScanPage'

const open = () => renderPage(<ScanPage />, { route: '/filament/scan', path: '/filament/scan' })
const json = (text: string) => new TextEncoder().encode(text)
const OPENSPOOL_PLA = json('{"protocol":"openspool","version":"1.0","type":"PLA","color_hex":"FFAABB","brand":"Generic","min_temp":"220","max_temp":"240"}')

/** Each API request the page sends, as `METHOD path` with its JSON body. */
function sent() {
  const requests: { line: string; body: unknown }[] = []
  const listener = ({ request }: { request: Request }) => {
    const url = new URL(request.url)
    const copy = request.clone()
    void copy.text().then((text) => {
      const parse = (): unknown => {
        try {
          return text ? JSON.parse(text) : null
        } catch {
          return text
        }
      }
      requests.push({ line: `${request.method} ${url.pathname}`, body: parse() })
    })
  }
  server.events.on('request:start', listener)
  return { requests, off: () => server.events.removeListener('request:start', listener) }
}

describe('the scan page without Web NFC (#7273)', () => {
  it('asks for Chrome on Android with a copyable link when Web NFC is missing', async () => {
    const { user } = open()
    expect(screen.getByRole('heading', { level: 1, name: 'Scan a tag' })).toBeInTheDocument()
    expect(screen.getByText('Open this page in Chrome on Android')).toBeInTheDocument()
    expect(screen.queryByText(/in its own tab, not inside Bambuddy/)).toBeNull()
    const field = screen.getByRole('textbox', { name: "This page's address" })
    expect(field).toHaveValue(window.location.href)
    expect(field).toHaveAttribute('readonly')
    await user.click(screen.getByRole('button', { name: 'Copy' }))
    expect(await navigator.clipboard.readText()).toBe(window.location.href)
    expect(await screen.findByText('Copied')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Scan' })).toBeNull()
  })

  it('says to open it in its own tab inside a frame', () => {
    const stub = installNdefStub()
    const top = window.top
    Object.defineProperty(window, 'top', { value: {}, configurable: true })
    try {
      open()
      expect(screen.getByText('Open this page in Chrome on Android')).toBeInTheDocument()
      expect(screen.getByText(/in its own tab, not inside Bambuddy/)).toBeInTheDocument()
    } finally {
      Object.defineProperty(window, 'top', { value: top, configurable: true })
      stub.restore()
    }
  })
})

describe('the scan page (#7273)', () => {
  let stub: NdefStub
  beforeEach(() => {
    stub = installNdefStub()
  })
  afterEach(() => stub.restore())

  it('starts one scan and lists each read', async () => {
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    const on = await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    expect(on).toHaveAttribute('aria-pressed', 'true')
    stub.tap({ serialNumber: '04:a2:00:11:22:33:44', records: [] })
    const reads = await screen.findByRole('list', { name: "This session's reads" })
    expect(await within(reads).findByText('New tag')).toBeInTheDocument()
    expect(within(reads).getByText('04:a2:00:11:22:33:44')).toBeInTheDocument()
    stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
    await waitFor(() => expect(within(reads).getAllByRole('listitem')).toHaveLength(2))
    // The newest read is at the top.
    expect(within(within(reads).getAllByRole('listitem')[0]!).getByText('Spool #3')).toBeInTheDocument()
    expect(stub.scans).toBe(1)
    // Tapping an older read opens it again.
    await user.click(within(reads).getByRole('button', { name: /New tag/ }))
    expect(await screen.findByRole('link', { name: 'Make the spool by hand' })).toHaveAttribute('href', filamentLinks.newSpool)
    expect(screen.queryByRole('button', { name: 'Create a spool from this tag' })).toBeNull()
  })

  it('opens a known tag on its spool card', async () => {
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
    const card = await screen.findByRole('region', { name: 'Spool #3' })
    expect(within(card).getByText(/^PLA/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open spool' })).toHaveAttribute('href', filamentLinks.spool(spoolId(3)))
    expect(screen.getByRole('button', { name: 'Write tag' })).toBeDisabled()
    expect(screen.getByText('Filament label')).toBeInTheDocument()
    expect(screen.getByText('Spool label')).toBeInTheDocument()
    expect(await screen.findByLabelText('Gross weight (g)')).toBeInTheDocument()
  })

  it("creates a spool from a new tag's draft and binds it", async () => {
    const log = sent()
    try {
      const { user } = open()
      await user.click(screen.getByRole('button', { name: 'Scan' }))
      await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
      stub.tap({ serialNumber: '04:77:66:55:44:33:22', records: [{ recordType: 'mime', mediaType: 'application/json', data: OPENSPOOL_PLA }] })
      const decoded = await screen.findByRole('region', { name: 'On the tag' })
      expect(within(decoded).getByText('OpenSpool 1.0')).toBeInTheDocument()
      expect(within(decoded).getByText('PLA')).toBeInTheDocument()
      expect(within(decoded).getByText('Generic')).toBeInTheDocument()
      expect(within(decoded).getByText('220–240 °C')).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Create a spool from this tag' }))
      const card = await screen.findByRole('region', { name: /^Spool #\d+$/ })
      expect(card).toBeInTheDocument()
      await waitFor(() => {
        const lines = log.requests.map((one) => one.line)
        const create = lines.indexOf('POST /api/v1/filament/spools')
        expect(create).toBeGreaterThan(-1)
        const binds = log.requests.filter((one) => one.line === 'POST /api/v1/filament/tags/reads' && (one.body as { bind_spool_id?: string }).bind_spool_id)
        expect(binds).toHaveLength(1)
        expect(lines.lastIndexOf('POST /api/v1/filament/tags/reads')).toBeGreaterThan(create)
      })
      const tag = (await listTags()).find((one) => one.tag_uid === '04776655443322')!
      expect(tag.kind).toBe('ntag_openspool')
      expect(tag.binding).not.toBeNull()
    } finally {
      log.off()
    }
  })

  it('binds a new tag to a searched spool', async () => {
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    stub.tap({ serialNumber: '04:10:20:30:40:50:60', records: [] })
    const search = await screen.findByRole('searchbox', { name: 'Bind to a spool' })
    await user.type(search, 'Teal')
    const matches = await screen.findByRole('list', { name: 'Spools found' })
    await user.click(within(matches).getByRole('button', { name: /#6/ }))
    expect(await screen.findByRole('region', { name: 'Spool #6' })).toBeInTheDocument()
    const tag = (await listTags()).find((one) => one.tag_uid === '04102030405060')!
    expect(tag.spool_number).toBe(6)
  })

  it('says why NFC was refused', async () => {
    stub.failScan(new DOMException('', 'NotAllowedError'))
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("Allow NFC for this site in Chrome's settings, then tap Scan again.")
    expect(screen.getByRole('button', { name: 'Scan' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('says a tag with no serial number cannot be kept', async () => {
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    stub.tap({ serialNumber: '', records: [{ recordType: 'mime', mediaType: 'application/json', data: OPENSPOOL_PLA }] })
    expect(await screen.findByText('This tag gave no serial number, so ScadBuddy cannot keep it.')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'On the tag' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create a spool from this tag' })).toBeNull()
  })

  it('stops scanning when it unmounts', async () => {
    const { user, unmount } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    expect(stub.scanning).toBe(true)
    unmount()
    expect(stub.scanning).toBe(false)
  })
})
