import { render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { listTags } from '../../api/filament'
import { L, spoolId } from '../../mocks/features/filament/seed'
import { seedNfcJob } from '../../mocks/features/filament/tags'
import { server } from '../../mocks/server'
import { filamentLinks } from '../../lib/filamentLinks'
import { installNdefStub, type NdefStub } from '../../test/ndefStub'
import { HttpResponse, http } from 'msw'
import { MemoryRouter, Route, Routes } from 'react-router'
import { renderPage } from '../../test/utils'
import { ScanPage } from './ScanPage'

const open = () => renderPage(<ScanPage />, { route: '/scan', path: '/scan' })
const json = (text: string) => new TextEncoder().encode(text)
/** A place label's `url` record, as Web NFC hands the page a URL's bytes. */
const PLACE_LABEL = { recordType: 'url', data: json('https://scadbuddy.example/x/L-4?from=label') }
const OPEN_AT_ONCE_KEY = 'scadbuddy.scan.openAtOnce'
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
    expect(screen.getByRole('heading', { level: 1, name: 'Scan' })).toBeInTheDocument()
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

  it('has no back link and names what it reads', () => {
    open()
    expect(screen.queryByRole('link', { name: 'Spools' })).not.toBeInTheDocument()
    expect(screen.getByText('Hold a tag or a label to the phone: a spool\'s tag, a place\'s tag, or a label with a code such as L-12 or S-142.')).toBeInTheDocument()
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
    // The label says it is on; a toggle state would claim a second tap turns it off.
    expect(on).not.toHaveAttribute('aria-pressed')
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
    expect(screen.getByRole('button', { name: 'Write tag' })).toBeEnabled()
    expect(screen.getByText('Filament label')).toBeInTheDocument()
    expect(screen.getByText('Spool label')).toBeInTheDocument()
    expect(await screen.findByLabelText('Gross weight (g)')).toBeInTheDocument()
  })

  it.each([
    { status: 503, code: 'mifare_needs_core' },
    { status: 422, code: 'mifare_unreadable' },
    { status: 422, code: 'mifare_invalid' },
  ])("posts the app's blocks, and the UID and records again on a $status $code (#8148)", async ({ status, code }) => {
    const log = sent()
    server.use(
      http.post('/api/v1/filament/tags/reads', async ({ request }) => {
        const body = (await request.clone().json()) as { mifare_blocks?: unknown }
        if (body.mifare_blocks)
          return HttpResponse.json(
            { type: 'about:blank', title: 'Refused', status, detail: 'core does not decode this read', code },
            { status, headers: { 'Content-Type': 'application/problem+json' } },
          )
        // Falls through to the filament mock's own answer.
      }),
    )
    window.ScadBuddyNfc = { native: true, lastMifare: { uid: '04A1B2C3D4E5F6', blocks: [{ index: 1, dataHex: '4131372d503100004746413137000000' }] } }
    try {
      const { user } = open()
      await user.click(screen.getByRole('button', { name: 'Scan' }))
      await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
      stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
      expect(await screen.findByRole('region', { name: 'Spool #3' })).toBeInTheDocument()
      expect(screen.queryByText(/could not look up the tag/)).toBeNull()
      const reads = log.requests.filter((one) => one.line === 'POST /api/v1/filament/tags/reads').map((one) => one.body)
      expect(reads).toEqual([
        { uid: '04A1B2C3D4E5F6', tag_type: 'mifare_classic', mifare_blocks: [{ index: 1, data_hex: '4131372d503100004746413137000000' }], records: [] },
        { uid: '04:a1:b2:c3:d4:e5:f6', tag_type: null, records: [] },
      ])
    } finally {
      log.off()
      delete window.ScadBuddyNfc
    }
  })

  it("shows another refusal of the app's blocks and does not post again (#8148)", async () => {
    const log = sent()
    server.use(
      http.post('/api/v1/filament/tags/reads', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Refused', status: 422, detail: 'no such spool', code: 'spool_unknown' },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    window.ScadBuddyNfc = { native: true, lastMifare: { uid: '04A1B2C3D4E5F6', blocks: [{ index: 1, dataHex: '4131372d503100004746413137000000' }] } }
    try {
      const { user } = open()
      await user.click(screen.getByRole('button', { name: 'Scan' }))
      await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
      stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
      expect(await screen.findByText(/could not look up the tag/)).toBeInTheDocument()
      const reads = log.requests.filter((one) => one.line === 'POST /api/v1/filament/tags/reads')
      expect(reads).toHaveLength(1)
    } finally {
      log.off()
      delete window.ScadBuddyNfc
    }
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

  it('announces each read', async () => {
    const { user } = open()
    const status = screen.getByRole('status', { name: 'Last read' })
    expect(status).toHaveTextContent('')
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
    await waitFor(() => expect(status).toHaveTextContent('Read 04:a1:b2:c3:d4:e5:f6: spool #3'))
    stub.tap({ serialNumber: '04:a2:00:11:22:33:45', records: [] })
    await waitFor(() => expect(status).toHaveTextContent('Read 04:a2:00:11:22:33:45: new tag'))
  })

  it('announces a second read of the same tag too', async () => {
    const { user } = open()
    const status = screen.getByRole('status', { name: 'Last read' })
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
    await waitFor(() => expect(status).toHaveTextContent('Read 04:a1:b2:c3:d4:e5:f6: spool #3'))
    const first = status.textContent
    stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
    await waitFor(() => expect(within(screen.getByRole('list', { name: "This session's reads" })).getAllByRole('listitem')).toHaveLength(2))
    // A live region says nothing when its text stays the same.
    await waitFor(() => expect(status.textContent).not.toBe(first))
    expect(status).toHaveTextContent('Read 04:a1:b2:c3:d4:e5:f6: spool #3')
  })

  it('says it is scanning when an agent job started the scan', async () => {
    seedNfcJob({ kind: 'read' })
    const { user } = open()
    const banner = await screen.findByRole('region', { name: 'Agent job' })
    const arm = within(banner).getByRole('button', { name: 'Arm' })
    await waitFor(() => expect(arm).toBeEnabled())
    await user.click(arm)
    expect(await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })).toBeInTheDocument()
  })

  it('says when a tag could not be read', async () => {
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    stub.tapError()
    expect(await screen.findByRole('alert')).toHaveTextContent('This tag could not be read. Hold it to the phone again.')
  })

  it('gives every read of a tag the bind, so an older read offers no second spool', async () => {
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    const tag = { serialNumber: '04:55:44:33:22:11:00', records: [{ recordType: 'mime', mediaType: 'application/json', data: OPENSPOOL_PLA }] }
    stub.tap(tag)
    const reads = await screen.findByRole('list', { name: "This session's reads" })
    await within(reads).findByText('New tag')
    stub.tap(tag)
    await waitFor(() => expect(within(reads).getAllByRole('listitem')).toHaveLength(2))
    await user.click(await screen.findByRole('button', { name: 'Create a spool from this tag' }))
    const card = await screen.findByRole('region', { name: /^Spool #\d+$/ })
    const name = card.getAttribute('aria-label')!
    // The older read of the same tag shows the spool too, not "Create" again.
    await waitFor(() => expect(within(reads).queryByText('New tag')).toBeNull())
    await user.click(within(reads).getAllByRole('button')[1]!)
    expect(await screen.findByRole('region', { name })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create a spool from this tag' })).toBeNull()
  })

  it('shows the new grams on the card after a weighing', async () => {
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
    const card = await screen.findByRole('region', { name: 'Spool #3' })
    expect(within(card).getByText('930 g left')).toBeInTheDocument()
    await user.type(await screen.findByLabelText('Gross weight (g)'), '1050')
    await user.click(screen.getByRole('button', { name: 'Save weight' }))
    expect(await within(card).findByText('800 g left')).toBeInTheDocument()
  })

  it('writes the tag of a known spool', async () => {
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
    await screen.findByRole('region', { name: 'Spool #3' })
    await user.click(screen.getByRole('button', { name: 'Write tag' }))
    await screen.findByText('Hold the tag still. If asked, lift it and tap again.')
    stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
    const steps = screen.getByRole('list', { name: 'Steps' })
    const readBack = within(steps).getByText('Read back').closest('li')!
    await waitFor(() => expect(readBack).toHaveAttribute('aria-current', 'step'))
    const written = stub.writes[0]!
    stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: written.map((one) => ({ recordType: one.recordType, mediaType: one.mediaType, data: one.data as Uint8Array })) })
    expect(await screen.findByText('Written and checked.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('status', { name: 'Last read' })).toHaveTextContent('Written and checked: spool #3'))
    // The write's taps are not reads.
    expect(within(screen.getByRole('list', { name: "This session's reads" })).getAllByRole('listitem')).toHaveLength(1)
    const tag = (await listTags()).find((one) => one.tag_uid === '04A1B2C3D4E5F6')!
    expect(tag.kind).toBe('ntag_openspool')
  })

  it('moves to the new tag after a spool is written onto it, and the old tag no longer claims the spool', async () => {
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
    await screen.findByRole('region', { name: 'Spool #3' })
    await user.click(screen.getByRole('button', { name: 'Write tag' }))
    await screen.findByText('Hold the tag still. If asked, lift it and tap again.')
    // A blank tag the session has not read.
    stub.tap({ serialNumber: '04:0b:0b:0b:0b:0b:0b', records: [] })
    const readBack = within(screen.getByRole('list', { name: 'Steps' })).getByText('Read back').closest('li')!
    await waitFor(() => expect(readBack).toHaveAttribute('aria-current', 'step'))
    const written = stub.writes[0]!
    stub.tap({ serialNumber: '04:0b:0b:0b:0b:0b:0b', records: written.map((one) => ({ recordType: one.recordType, mediaType: one.mediaType, data: one.data as Uint8Array })) })
    const reads = screen.getByRole('list', { name: "This session's reads" })
    await waitFor(() => expect(within(reads).getAllByRole('listitem')).toHaveLength(2))
    const [newer, older] = within(reads).getAllByRole('listitem')
    // The written tag is opened and holds the spool; the old one is a new tag now.
    expect(within(newer!).getByText('04:0b:0b:0b:0b:0b:0b')).toBeInTheDocument()
    expect(within(newer!).getByRole('button')).toHaveAttribute('aria-current', 'true')
    expect(within(newer!).getByText('Spool #3')).toBeInTheDocument()
    await waitFor(() => expect(within(older!).getByText('New tag')).toBeInTheDocument())
    expect(await screen.findByRole('region', { name: 'Spool #3' })).toBeInTheDocument()
  })

  it('moves a spool onto a tag already in the list without listing that tag twice', async () => {
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
    await screen.findByRole('region', { name: 'Spool #3' })
    stub.tap({ serialNumber: '04:0b:0b:0b:0b:0b:0b', records: [] })
    const reads = screen.getByRole('list', { name: "This session's reads" })
    await waitFor(() => expect(within(reads).getAllByRole('listitem')).toHaveLength(2))
    // Open the spool's read again and write the spool onto the listed blank tag.
    await user.click(within(reads).getAllByRole('button')[1]!)
    await user.click(await screen.findByRole('button', { name: 'Write tag' }))
    await screen.findByText('Hold the tag still. If asked, lift it and tap again.')
    stub.tap({ serialNumber: '04:0b:0b:0b:0b:0b:0b', records: [] })
    const readBack = within(screen.getByRole('list', { name: 'Steps' })).getByText('Read back').closest('li')!
    await waitFor(() => expect(readBack).toHaveAttribute('aria-current', 'step'))
    const written = stub.writes[0]!
    stub.tap({ serialNumber: '04:0b:0b:0b:0b:0b:0b', records: written.map((one) => ({ recordType: one.recordType, mediaType: one.mediaType, data: one.data as Uint8Array })) })
    await waitFor(() => expect(within(reads).getAllByText('Spool #3')).toHaveLength(1))
    await waitFor(() => expect(within(reads).getAllByText('New tag')).toHaveLength(1))
    const items = within(reads).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    const blank = items.find((one) => within(one).queryByText('04:0b:0b:0b:0b:0b:0b'))!
    expect(within(blank).getByText('Spool #3')).toBeInTheDocument()
    expect(within(blank).getByRole('button')).toHaveAttribute('aria-current', 'true')
  })

  it('says why NFC was refused', async () => {
    stub.failScan(new DOMException('', 'NotAllowedError'))
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("Allow NFC for this site in Chrome's settings, then tap Scan again.")
    expect(screen.getByRole('button', { name: 'Scan' })).not.toHaveAttribute('aria-pressed')
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

  it("runs an agent's read job, and the tap stays out of this session's reads", async () => {
    const job = seedNfcJob({ kind: 'read' })
    const log = sent()
    try {
      const { user } = open()
      const status = screen.getByRole('status', { name: 'Last read' })
      const banner = await screen.findByRole('region', { name: 'Agent job' })
      expect(within(banner).getByRole('heading', { name: 'Agent wants to read a tag' })).toBeInTheDocument()
      const arm = within(banner).getByRole('button', { name: 'Arm' })
      await waitFor(() => expect(arm).toBeEnabled())
      await user.click(arm)
      await within(banner).findByText('Hold a tag to the phone')
      stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
      await waitFor(() => expect(status).toHaveTextContent('Agent job done: the tag was read.'))
      expect(log.requests.map((one) => one.line)).toContain(`POST /api/v1/filament/nfc-jobs/${job.id}/result`)
      expect(log.requests.map((one) => one.line)).not.toContain('POST /api/v1/filament/tags/reads')
      expect(screen.queryByRole('list', { name: "This session's reads" })).toBeNull()
    } finally {
      log.off()
    }
  })

  it('a tapped place label opens its card', async () => {
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    stub.tap({ serialNumber: '04:0c:0c:0c:0c:0c:0c', records: [PLACE_LABEL] })
    const card = await screen.findByRole('region', { name: 'Place L-4' })
    expect(within(card).getByRole('link', { name: 'Open' })).toHaveAttribute('href', filamentLinks.location(L.AMS_A1))
    const reads = screen.getByRole('list', { name: "This session's reads" })
    expect(within(reads).getByText('Place L-4')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create a spool from this tag' })).toBeNull()
  })

  it('announces a place label by its code', async () => {
    const { user } = open()
    await user.click(screen.getByRole('button', { name: 'Scan' }))
    await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
    stub.tap({ serialNumber: '04:0c:0c:0c:0c:0c:0c', records: [PLACE_LABEL] })
    await waitFor(() => expect(screen.getByRole('status', { name: 'Last read' })).toHaveTextContent('Read 04:0c:0c:0c:0c:0c:0c: place L-4 (read 1)'))
  })

  describe('Open what I scan at once', () => {
    beforeEach(() => localStorage.clear())

    /** The page with a stand-in for the place's and the spool's pages, so a navigation shows. */
    const openWithTargets = () =>
      render(
        <MemoryRouter initialEntries={['/scan']}>
          <Routes>
            <Route path="/scan" element={<ScanPage />} />
            <Route path="/filament/locations/:id" element={<h1>Place page</h1>} />
            <Route path="/filament/spools/:id" element={<h1>Spool page</h1>} />
          </Routes>
        </MemoryRouter>,
      )

    it('is off by default and opens nothing by itself', async () => {
      const { user } = open()
      expect(screen.getByRole('switch', { name: 'Open what I scan at once' })).not.toBeChecked()
      await user.click(screen.getByRole('button', { name: 'Scan' }))
      await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
      stub.tap({ serialNumber: '04:0c:0c:0c:0c:0c:0c', records: [PLACE_LABEL] })
      expect(await screen.findByRole('region', { name: 'Place L-4' })).toBeInTheDocument()
    })

    it('with Open at once on, a tapped place label navigates to the place', async () => {
      openWithTargets()
      const { default: userEvent } = await import('@testing-library/user-event')
      const user = userEvent.setup()
      await user.click(screen.getByRole('switch', { name: 'Open what I scan at once' }))
      await user.click(screen.getByRole('button', { name: 'Scan' }))
      await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
      stub.tap({ serialNumber: '04:0c:0c:0c:0c:0c:0c', records: [PLACE_LABEL] })
      expect(await screen.findByRole('heading', { name: 'Place page' })).toBeInTheDocument()
    })

    it("with Open at once on, a known tag navigates to its spool", async () => {
      openWithTargets()
      const { default: userEvent } = await import('@testing-library/user-event')
      const user = userEvent.setup()
      await user.click(screen.getByRole('switch', { name: 'Open what I scan at once' }))
      await user.click(screen.getByRole('button', { name: 'Scan' }))
      await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
      stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
      expect(await screen.findByRole('heading', { name: 'Spool page' })).toBeInTheDocument()
    })

    it('with Open at once on, two taps in flight navigate once, to the first', async () => {
      openWithTargets()
      const { default: userEvent } = await import('@testing-library/user-event')
      const user = userEvent.setup()
      await user.click(screen.getByRole('switch', { name: 'Open what I scan at once' }))
      await user.click(screen.getByRole('button', { name: 'Scan' }))
      await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
      // The second read's answer is held until the first has opened its page, then let go.
      let release!: () => void
      const released = new Promise<void>((resolve) => {
        release = resolve
      })
      server.use(
        http.post('/api/v1/filament/tags/reads', async ({ request }) => {
          const body = (await request.clone().json()) as { uid: string | null }
          if (body.uid === '04:a1:b2:c3:d4:e5:f6') await released
          // Falls through to the filament mock's own answer.
        }),
      )
      stub.tap({ serialNumber: '04:0c:0c:0c:0c:0c:0c', records: [PLACE_LABEL] })
      stub.tap({ serialNumber: '04:a1:b2:c3:d4:e5:f6', records: [] })
      expect(await screen.findByRole('heading', { name: 'Place page' })).toBeInTheDocument()
      release()
      // The answer that would open the spool lands on a page that is gone, and is dropped.
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(screen.queryByRole('heading', { name: 'Spool page' })).toBeNull()
      expect(screen.getByRole('heading', { name: 'Place page' })).toBeInTheDocument()
    })

    it('with Open at once on, a new tag stays on the page', async () => {
      openWithTargets()
      const { default: userEvent } = await import('@testing-library/user-event')
      const user = userEvent.setup()
      await user.click(screen.getByRole('switch', { name: 'Open what I scan at once' }))
      await user.click(screen.getByRole('button', { name: 'Scan' }))
      await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
      stub.tap({ serialNumber: '04:0c:0c:0c:0c:0c:0c', records: [] })
      // Nothing to open: the New tag card (a blank tag has no draft, so it offers the form).
      expect(await screen.findByRole('link', { name: 'Make the spool by hand' })).toBeInTheDocument()
      expect(screen.queryByRole('heading', { name: 'Place page' })).toBeNull()
      expect(screen.getByRole('heading', { name: 'Scan', level: 1 })).toBeInTheDocument()
    })

    it('is remembered, and a page opened with it on starts the scan by itself', async () => {
      const first = open()
      await first.user.click(screen.getByRole('switch', { name: 'Open what I scan at once' }))
      expect(localStorage.getItem(OPEN_AT_ONCE_KEY)).toBe('1')
      first.unmount()
      stub.restore()
      stub = installNdefStub()
      open()
      expect(screen.getByRole('switch', { name: 'Open what I scan at once' })).toBeChecked()
      await screen.findByRole('button', { name: 'Scanning — hold a tag to the phone' })
      expect(stub.scans).toBe(1)
    })

    it('a refused auto-start shows the Scan button and the reason', async () => {
      localStorage.setItem(OPEN_AT_ONCE_KEY, '1')
      stub.failScan(new DOMException('', 'NotAllowedError'))
      open()
      expect(await screen.findByRole('alert')).toHaveTextContent("Allow NFC for this site in Chrome's settings, then tap Scan again.")
      expect(screen.getByRole('button', { name: 'Scan' })).toBeEnabled()
      expect(stub.scans).toBe(1)
    })
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
