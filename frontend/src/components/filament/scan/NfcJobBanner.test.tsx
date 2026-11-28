import { act, screen, waitFor, within } from '@testing-library/react'
import { delay, http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { seedNfcJob } from '../../../mocks/features/filament/tags'
import { server } from '../../../mocks/server'
import { NfcSession, recordsSha256, type WireRecord } from '../../../lib/webNfc'
import { installNdefStub, type NdefStub, type StubReading } from '../../../test/ndefStub'
import { renderPage } from '../../../test/utils'
import { NfcJobBanner } from './NfcJobBanner'

const UID = '04:a1:b2:c3:d4:e5:f6'
const hexOfText = (text: string) => Array.from(new TextEncoder().encode(text), (b) => b.toString(16).padStart(2, '0')).join('')
const bytesOf = (hex: string) => new Uint8Array(hex.match(/../g)?.map((pair) => parseInt(pair, 16)) ?? [])
const RECORDS: WireRecord[] = [
  { record_type: 'text', encoding: 'utf-8', lang: 'en', data_hex: hexOfText('Shelf B3, bin 2') },
  { record_type: 'mime', media_type: 'application/json', data_hex: hexOfText('{"bin":"B3-2"}') },
]
const OLD = { recordType: 'text', data: new TextEncoder().encode('old') }
const tapOf = (serialNumber: string, records: WireRecord[]): StubReading => ({
  serialNumber,
  records: records.map((one) => ({ recordType: one.record_type, ...(one.media_type ? { mediaType: one.media_type } : {}), data: bytesOf(one.data_hex) })),
})

/** Each job request the banner sends, as `METHOD path` with its JSON body. */
function sent() {
  const requests: { line: string; body: unknown }[] = []
  const listener = ({ request }: { request: Request }) => {
    const url = new URL(request.url)
    void request
      .clone()
      .text()
      .then((text) => requests.push({ line: `${request.method} ${url.pathname}`, body: text ? (JSON.parse(text) as unknown) : null }))
  }
  server.events.on('request:start', listener)
  return {
    requests,
    lines: () => requests.map((one) => one.line),
    bodyOf: (line: string) => requests.find((one) => one.line === line)?.body,
    bodiesOf: (line: string) => requests.filter((one) => one.line === line).map((one) => one.body),
    off: () => server.events.removeListener('request:start', listener),
  }
}

const JOBS = '/api/v1/filament/nfc-jobs'

describe('NfcJobBanner (#7273)', () => {
  let stub: NdefStub
  let session: NfcSession
  let log: ReturnType<typeof sent>
  beforeEach(async () => {
    stub = installNdefStub()
    session = new NfcSession()
    await session.start()
    log = sent()
  })
  afterEach(() => {
    log.off()
    session.stop()
    stub.restore()
  })

  const render = (onAnnounce = vi.fn()) => ({ ...renderPage(<NfcJobBanner session={session} onAnnounce={onAnnounce} />), onAnnounce })
  const armButton = async () => {
    const arm = await screen.findByRole('button', { name: 'Arm' })
    await waitFor(() => expect(arm).toBeEnabled())
    return arm
  }

  it('shows a pending job with its records and countdown', async () => {
    seedNfcJob({ kind: 'write', records: RECORDS, ttl_s: 9 * 60 + 12 })
    render()
    const banner = await screen.findByRole('region', { name: 'Agent job' })
    expect(within(banner).getByRole('heading', { name: 'Agent wants to write 2 records' })).toBeInTheDocument()
    expect(within(banner).getByText(/Expires in 9:1\d/)).toBeInTheDocument()
    const records = within(banner).getByRole('list', { name: 'Records to write' })
    const items = within(records).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('text')
    expect(items[0]).toHaveTextContent('15 bytes')
    expect(items[0]).toHaveTextContent('Shelf B3, bin 2')
    expect(items[1]).toHaveTextContent('application/json')
    expect(items[1]).toHaveTextContent('14 bytes')
    expect(items[1]).toHaveTextContent('"bin": "B3-2"')
    expect(await armButton()).toBeEnabled()
    expect(within(banner).getByRole('button', { name: 'Decline' })).toBeEnabled()
  })

  it('says a read job wants a tag read', async () => {
    seedNfcJob({ kind: 'read' })
    render()
    expect(await screen.findByRole('heading', { name: 'Agent wants to read a tag' })).toBeInTheDocument()
  })

  it('polls every 2 s only while visible', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const visibility = (state: 'visible' | 'hidden') => {
      Object.defineProperty(document, 'visibilityState', { value: state, configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    }
    const gets = () => log.lines().filter((line) => line === `GET ${JOBS}`).length
    try {
      render()
      await vi.waitFor(() => expect(gets()).toBe(1))
      await act(() => vi.advanceTimersByTimeAsync(2000))
      await vi.waitFor(() => expect(gets()).toBe(2))
      act(() => visibility('hidden'))
      await act(() => vi.advanceTimersByTimeAsync(6000))
      expect(gets()).toBe(2)
      act(() => visibility('visible'))
      await vi.waitFor(() => expect(gets()).toBe(3))
      await act(() => vi.advanceTimersByTimeAsync(2000))
      await vi.waitFor(() => expect(gets()).toBe(4))
    } finally {
      Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
      vi.useRealTimers()
    }
  })

  it('arms a read job and posts what the tag held', async () => {
    const job = seedNfcJob({ kind: 'read' })
    const { user, onAnnounce } = render()
    await user.click(await armButton())
    expect(await screen.findByText('Hold a tag to the phone')).toBeInTheDocument()
    expect(log.bodyOf(`POST ${JOBS}/${job.id}/claim`)).toEqual({})
    stub.tap({ serialNumber: UID, records: [OLD] })
    await waitFor(() => expect(log.bodyOf(`POST ${JOBS}/${job.id}/result`)).toEqual({ uid: UID, records: [expect.objectContaining({ record_type: 'text', data_hex: hexOfText('old') })] }))
    await waitFor(() => expect(onAnnounce).toHaveBeenLastCalledWith('Agent job done: the tag was read.'))
  })

  it("posts the chip the ScadBuddy app read with a read job's result (#8366)", async () => {
    const job = seedNfcJob({ kind: 'read' })
    const { user } = render()
    await user.click(await armButton())
    expect(await screen.findByText('Hold a tag to the phone')).toBeInTheDocument()
    stub.tap({ serialNumber: UID, records: [], tag: { chip: 'NTAG215', user_bytes: 504, max_ndef_bytes: 496, writable: true } })
    await waitFor(() => expect(log.bodyOf(`POST ${JOBS}/${job.id}/result`)).toEqual({ uid: UID, records: [], chip: 'NTAG215', max_ndef_bytes: 496, writable: true }))
  })

  it('arms a write job, backs up with the job id, writes the records and posts the read-back', async () => {
    const job = seedNfcJob({ kind: 'write', records: RECORDS })
    const { user, onAnnounce } = render()
    await user.click(await armButton())
    await waitFor(() => expect(log.bodyOf(`POST ${JOBS}/${job.id}/claim`)).toEqual({ records_sha256: expect.any(String) }))
    expect((log.bodyOf(`POST ${JOBS}/${job.id}/claim`) as { records_sha256: string }).records_sha256).toBe(await recordsSha256(job.records!))
    expect(await screen.findByText('Hold a tag to the phone')).toBeInTheDocument()
    stub.tap({ serialNumber: UID, records: [OLD] })
    await waitFor(() => expect(stub.writes).toHaveLength(1))
    expect(log.bodyOf('POST /api/v1/filament/tags/backups')).toMatchObject({ uid: UID, reason: 'agent_write', job_id: job.id })
    expect(stub.writes[0]!.map((one) => one.recordType)).toEqual(['text', 'mime'])
    stub.tap(tapOf(UID, RECORDS))
    await waitFor(() => expect(log.bodyOf(`POST ${JOBS}/${job.id}/result`)).toMatchObject({ uid: UID, backup_id: expect.any(String) }))
    const result = log.bodyOf(`POST ${JOBS}/${job.id}/result`) as { records: WireRecord[] }
    expect(result.records.map((one) => one.data_hex)).toEqual(RECORDS.map((one) => one.data_hex))
    expect(await screen.findByText('Written and checked.')).toBeInTheDocument()
    expect(onAnnounce).toHaveBeenLastCalledWith('Agent job done: Written and checked.')
  })

  it('declines a job', async () => {
    const job = seedNfcJob({ kind: 'write', records: RECORDS })
    const { user } = render()
    await armButton()
    await user.click(screen.getByRole('button', { name: 'Decline' }))
    await waitFor(() => expect(log.bodyOf(`POST ${JOBS}/${job.id}/result`)).toEqual({ error: 'declined by the person' }))
    expect(log.bodyOf(`POST ${JOBS}/${job.id}/claim`)).toEqual({ records_sha256: await recordsSha256(job.records!) })
    expect(stub.writes).toHaveLength(0)
    await waitFor(() => expect(screen.queryByRole('heading', { name: /Agent wants/ })).toBeNull())
  })

  it("posts the browser's error as a failed job", async () => {
    const job = seedNfcJob({ kind: 'write', records: RECORDS })
    const { user } = render()
    await user.click(await armButton())
    await screen.findByText('Hold a tag to the phone')
    stub.failNextWrite(new DOMException('The tag moved away', 'NetworkError'))
    stub.tap({ serialNumber: UID, records: [OLD] })
    await waitFor(() => expect(log.bodyOf(`POST ${JOBS}/${job.id}/result`)).toEqual({ error: expect.stringContaining('The tag moved away') }))
    expect(await screen.findByText(/The tag was not written: The tag moved away/)).toBeInTheDocument()
  })

  it('says why a job cannot be written when it shows it, keeps Arm off and lets it be declined', async () => {
    // A smart poster that holds an absolute-URL record, which Web NFC refuses inside one.
    const job = seedNfcJob({ kind: 'write', records: [{ record_type: 'smart-poster', data_hex: 'd3010078' }] })
    const { user } = render()
    const banner = await screen.findByRole('region', { name: 'Agent job' })
    expect(await within(banner).findByText(/This job cannot be written from this page: the smart poster holds an absolute-URL record/)).toBeInTheDocument()
    const decline = within(banner).getByRole('button', { name: 'Decline' })
    await waitFor(() => expect(decline).toBeEnabled())
    expect(within(banner).getByRole('button', { name: 'Arm' })).toBeDisabled()
    expect(log.lines()).not.toContain(`POST ${JOBS}/${job.id}/claim`)
    await user.click(decline)
    await waitFor(() => expect(log.bodyOf(`POST ${JOBS}/${job.id}/result`)).toEqual({ error: 'declined by the person' }))
    expect(log.lines()).not.toContain('POST /api/v1/filament/tags/backups')
    expect(stub.writes).toHaveLength(0)
  })

  it('hashes the records once while the job list is polled', async () => {
    // Each poll answers a new job object; the hash is of the records, so it is not started again.
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    let finish: (() => void) | null = null
    const digest = crypto.subtle.digest.bind(crypto.subtle)
    const spy = vi.spyOn(crypto.subtle, 'digest').mockImplementation(
      (algorithm, data) =>
        new Promise((resolve) => {
          finish = () => void digest(algorithm, data).then(resolve)
        }),
    )
    try {
      seedNfcJob({ kind: 'write', records: RECORDS })
      render()
      await screen.findByRole('list', { name: 'Records to write' })
      for (let poll = 0; poll < 3; poll += 1) {
        const gets = log.lines().length
        await act(() => vi.advanceTimersByTimeAsync(2000))
        await vi.waitFor(() => expect(log.lines().length).toBeGreaterThan(gets))
      }
      expect(spy).toHaveBeenCalledTimes(1)
      act(() => finish!())
      await vi.waitFor(() => expect(screen.getByRole('button', { name: 'Arm' })).toBeEnabled())
    } finally {
      spy.mockRestore()
      vi.useRealTimers()
    }
  })

  it('ends the job when the read-back differs or another tag answers', async () => {
    const first = seedNfcJob({ kind: 'write', records: RECORDS })
    const { user } = render()
    await user.click(await armButton())
    await screen.findByText('Hold a tag to the phone')
    stub.tap({ serialNumber: UID, records: [OLD] })
    await waitFor(() => expect(stub.writes).toHaveLength(1))
    stub.tap(tapOf(UID, RECORDS.slice(0, 1)))
    // A read-back that differs goes to ScadBuddy as it is, so the job fails with what the tag holds.
    await waitFor(() =>
      expect(log.bodyOf(`POST ${JOBS}/${first.id}/result`)).toEqual({
        uid: UID,
        records: [expect.objectContaining({ record_type: 'text', data_hex: RECORDS[0]!.data_hex })],
        backup_id: expect.any(String),
      }),
    )
    await waitFor(() => expect(screen.getByText('The tag does not hold what was sent.')).toBeInTheDocument())

    const second = seedNfcJob({ kind: 'write', records: RECORDS })
    await user.click(await armButton())
    await waitFor(() => expect(log.lines()).toContain(`POST ${JOBS}/${second.id}/claim`))
    await screen.findByText('Hold a tag to the phone')
    stub.tap({ serialNumber: UID, records: [OLD] })
    await waitFor(() => expect(stub.writes).toHaveLength(2))
    stub.tap(tapOf('04:99:99:99:99:99:99', RECORDS))
    await waitFor(() => expect(log.bodyOf(`POST ${JOBS}/${second.id}/result`)).toEqual({ error: 'a different tag answered' }))
  })

  it('reloads and writes nothing when the job changed, then arms the new records', async () => {
    const job = seedNfcJob({ kind: 'write', records: RECORDS })
    const NEW: WireRecord[] = [{ record_type: 'text', media_type: null, id: null, encoding: 'utf-8', lang: 'en', data_hex: hexOfText('Shelf C1') }]
    server.use(
      http.post(
        `${JOBS}/:id/claim`,
        () => {
          job.records = NEW
          // The reload answers late, so a hash of the old records is ready first (M1).
          server.use(http.get(JOBS, () => delay(300), { once: true }))
          return HttpResponse.json({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'changed', code: 'records_mismatch' }, { status: 409, headers: { 'Content-Type': 'application/problem+json' } })
        },
        { once: true },
      ),
    )
    const { user } = render()
    await user.click(await armButton())
    expect(await screen.findByText('The job changed. Nothing was written.')).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Agent wants to write 1 record' })).toBeInTheDocument()
    expect(stub.writes).toHaveLength(0)
    await user.click(await armButton())
    expect(await screen.findByText('Hold a tag to the phone')).toBeInTheDocument()
    const claims = log.bodiesOf(`POST ${JOBS}/${job.id}/claim`)
    expect(claims).toHaveLength(2)
    expect(claims[1]).toEqual({ records_sha256: await recordsSha256(NEW) })
  })

  it('ends a read job with an error when ScadBuddy refuses the reading', async () => {
    const job = seedNfcJob({ kind: 'read' })
    // Keyed to this job: an earlier test's page may still end its own job on unmount.
    server.use(http.post(`${JOBS}/${job.id}/result`, () => HttpResponse.json({ type: 'about:blank', title: 'Server error', status: 500, detail: 'boom' }, { status: 500, headers: { 'Content-Type': 'application/problem+json' } }), { once: true }))
    const { user } = render()
    await user.click(await armButton())
    await screen.findByText('Hold a tag to the phone')
    stub.tap({ serialNumber: UID, records: [OLD] })
    await waitFor(() => expect(log.bodiesOf(`POST ${JOBS}/${job.id}/result`)).toHaveLength(2))
    expect(log.bodiesOf(`POST ${JOBS}/${job.id}/result`)[1]).toEqual({ error: expect.stringContaining('ScadBuddy did not take the reading') })
    await waitFor(() => expect(job.state).toBe('failed'))
  })

  it.each(['read', 'write'] as const)('stops a %s job the agent cancelled after Arm and frees the session', async (kind) => {
    const job = seedNfcJob(kind === 'read' ? { kind } : { kind, records: RECORDS })
    const { user, onAnnounce } = render()
    await user.click(await armButton())
    await screen.findByText('Hold a tag to the phone')
    await fetch(new URL(`${JOBS}/${job.id}/cancel`, window.location.origin), { method: 'POST' })
    expect(await screen.findByText('The agent stopped waiting.', {}, { timeout: 3500 })).toBeInTheDocument()
    expect(onAnnounce).toHaveBeenLastCalledWith('The agent stopped waiting.')
    expect(screen.queryByText('Hold a tag to the phone')).toBeNull()
    expect(log.lines()).not.toContain(`POST ${JOBS}/${job.id}/result`)
    const release = session.claim()
    expect(release).not.toBeNull()
    release?.()
  })

  it('announces when another write holds the session or NFC does not start', async () => {
    seedNfcJob({ kind: 'read' })
    const { user, onAnnounce } = render()
    const held = session.claim()!
    await user.click(await armButton())
    await waitFor(() => expect(onAnnounce).toHaveBeenLastCalledWith('Another write is running on this page. Arm the job once it ends.'))
    held()
    vi.spyOn(session, 'start').mockRejectedValueOnce(new Error('NFC is off'))
    await user.click(await armButton())
    await waitFor(() => expect(onAnnounce).toHaveBeenLastCalledWith('NFC could not start: NFC is off. Nothing was armed.'))
  })

  it('cuts a long error on a character boundary', async () => {
    const job = seedNfcJob({ kind: 'write', records: RECORDS })
    const { user } = render()
    await user.click(await armButton())
    await screen.findByText('Hold a tag to the phone')
    stub.failNextWrite(new DOMException('😀'.repeat(600), 'NetworkError'))
    stub.tap({ serialNumber: UID, records: [OLD] })
    await waitFor(() => expect(log.bodyOf(`POST ${JOBS}/${job.id}/result`)).toBeDefined())
    const { error } = log.bodyOf(`POST ${JOBS}/${job.id}/result`) as { error: string }
    expect([...error]).toHaveLength(500)
    expect(error).not.toMatch(/[\uD800-\uDFFF]/u)
  })

  describe('shows what every record holds, as plain text (R22)', () => {
    const shown = async (records: WireRecord[]) => {
      seedNfcJob({ kind: 'write', records })
      render()
      const list = await screen.findByRole('list', { name: 'Records to write' })
      // Decline, not Arm: a poster this page cannot write is shown too, with Arm off.
      const decline = await screen.findByRole('button', { name: 'Decline' })
      await waitFor(() => expect(decline).toBeEnabled())
      // The list's own items: a smart poster's records are a list inside its item.
      return Array.from(list.children) as HTMLElement[]
    }

    it('keeps JSON as it was sent unless pretty-printing loses nothing', async () => {
      const items = await shown([
        { record_type: 'mime', media_type: 'application/json', data_hex: hexOfText('{"a":1,"a":2}') },
        { record_type: 'mime', media_type: 'application/json', data_hex: hexOfText('{"n":12345678901234567890}') },
      ])
      expect(items[0]).toHaveTextContent('{"a":1,"a":2}')
      expect(items[1]).toHaveTextContent('{"n":12345678901234567890}')
    })

    it('shows a URL record and a smart poster as text, never a link', async () => {
      // The poster's message: a short "U" record (prefix 0x04 "https://") then a short "T" record ("en", no text).
      const poster = '91010c5504' + hexOfText('example.org') + '5101035402' + hexOfText('en')
      const items = await shown([
        { record_type: 'url', data_hex: hexOfText('https://example.com/x') },
        { record_type: 'smart-poster', data_hex: poster },
      ])
      expect(items[0]).toHaveTextContent('https://example.com/x')
      expect(items[1]).toHaveTextContent('https://example.org')
      expect(screen.queryByRole('link')).toBeNull()
    })

    it("shows every record in a smart poster, and a poster it cannot read as hex", async () => {
      // "U" (https://example.org), "T" ("en", "Shop") and "act" (0x00, do the action).
      const poster = '91010c5504' + hexOfText('example.org') + '1101075402' + hexOfText('en') + hexOfText('Shop') + '510301' + hexOfText('act') + '00'
      const items = await shown([
        { record_type: 'smart-poster', data_hex: poster },
        { record_type: 'smart-poster', data_hex: '9101' },
      ])
      const inner = within(items[0]!).getByRole('list', { name: 'Records in the smart poster' })
      const rows = within(inner).getAllByRole('listitem')
      expect(rows).toHaveLength(3)
      expect(rows[0]).toHaveTextContent('url')
      expect(rows[0]).toHaveTextContent('https://example.org')
      expect(rows[1]).toHaveTextContent('text')
      expect(rows[1]).toHaveTextContent('Shop')
      expect(rows[2]).toHaveTextContent(':act')
      expect(rows[2]).toHaveTextContent('00')
      expect(items[1]).toHaveTextContent('91 01')
    })

    it("shows each record's id, which is written too", async () => {
      const items = await shown([{ record_type: 'text', id: 'shelf-label', encoding: 'utf-8', lang: 'en', data_hex: hexOfText('B3') }])
      expect(items[0]).toHaveTextContent('id shelf-label')
    })

    it('says a record with no data has none', async () => {
      const items = await shown([{ record_type: 'text', encoding: 'utf-8', lang: 'en', data_hex: '' }])
      expect(items[0]).toHaveTextContent('No data')
    })

    it('shows an unknown record, and a text record in an encoding NDEF does not use, as hex', async () => {
      const items = await shown([
        { record_type: 'mime', media_type: 'application/octet-stream', data_hex: '0001ff' },
        { record_type: 'text', encoding: 'latin1', lang: 'en', data_hex: hexOfText('Hi there') },
        { record_type: 'unknown', data_hex: 'ab'.repeat(100) },
      ])
      expect(items[0]).toHaveTextContent('00 01 ff')
      expect(items[1]).toHaveTextContent(hexOfText('Hi there').match(/../g)!.join(' '))
      expect(items[1]).not.toHaveTextContent('Hi there')
      expect(items[2]).toHaveTextContent('100 bytes')
      expect(items[2]).toHaveTextContent('36 more bytes')
    })

    it('reads a UTF-16 text record with no BOM as big-endian, as an NFC reader does, and by its BOM otherwise', async () => {
      const items = await shown([
        { record_type: 'text', encoding: 'utf-16', lang: 'en', data_hex: '00480069' },
        { record_type: 'text', encoding: 'utf-16', lang: 'en', data_hex: 'fffe48006900' },
      ])
      expect(items[0]).toHaveTextContent('Hi')
      expect(items[1]).toHaveTextContent('Hi')
    })

    it('shows bidi, control and zero-width characters as escapes, never raw', async () => {
      const items = await shown([
        { record_type: 'url', data_hex: hexOfText('https://example.com/\u202Egpj.exe') },
        { record_type: 'text', encoding: 'utf-8', lang: 'en', data_hex: hexOfText('\uFEFFa\u200Bb\u0007c\u0085d\u2066e\u200Ff') },
      ])
      expect(items[0]).toHaveTextContent('https://example.com/⟨U+202E⟩gpj.exe')
      expect(items[0]!.textContent).not.toContain('\u202E')
      expect(items[1]).toHaveTextContent('⟨U+FEFF⟩a⟨U+200B⟩b⟨U+0007⟩c⟨U+0085⟩d⟨U+2066⟩e⟨U+200F⟩f')
    })
  })

  it('drops a job the agent stopped waiting on', async () => {
    const job = seedNfcJob({ kind: 'read' })
    const { user } = render()
    const arm = await armButton()
    await fetch(new URL(`${JOBS}/${job.id}/cancel`, window.location.origin), { method: 'POST' })
    await user.click(arm)
    expect(await screen.findByText('The agent stopped waiting.')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /Agent wants/ })).toBeNull()
  })

  it('neither stops nor says Expired when the time ends after the backup (#7419 review)', async () => {
    const job = seedNfcJob({ kind: 'write', records: RECORDS })
    const { user, onAnnounce } = render()
    await user.click(await armButton())
    expect(await screen.findByText('Hold a tag to the phone')).toBeInTheDocument()
    stub.tap({ serialNumber: UID, records: [OLD] })
    await waitFor(() => expect(stub.writes).toHaveLength(1))
    // The job's time ends while the tag is read back; the page reads the job's new `expires_at`.
    job.expires_at = new Date(Date.now() - 1000).toISOString()
    expect(await screen.findByText('Finishing the write', {}, { timeout: 3500 })).toBeInTheDocument()
    expect(screen.queryByText('Expired')).toBeNull()
    expect(onAnnounce).not.toHaveBeenCalledWith('Expired. Nothing was bound.')
    stub.tap(tapOf(UID, RECORDS))
    expect(await screen.findByText('Written and checked.')).toBeInTheDocument()
    expect(onAnnounce).toHaveBeenLastCalledWith('Agent job done: Written and checked.')
  }, 10_000)

  it('does not stop at 0:00 while it backs the tag up (#7419 round 2)', async () => {
    const job = seedNfcJob({ kind: 'write', records: RECORDS })
    const { user, onAnnounce } = render()
    await user.click(await armButton())
    expect(await screen.findByText('Hold a tag to the phone')).toBeInTheDocument()
    // The backup is kept at once but answers late; meanwhile the page reads the job's time as ended.
    let ended: string | null = null
    server.use(
      http.get(`${JOBS}/${job.id}`, () => HttpResponse.json(ended ? { ...job, expires_at: ended } : job)),
      http.post('/api/v1/filament/tags/backups', async ({ request }) => {
        const kept = await fetch(new URL('/api/v1/filament/tags/backups', window.location.origin), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: await request.text() })
        const body = (await kept.json()) as Record<string, string>
        await delay(3000)
        return HttpResponse.json(body, { status: kept.status })
      }, { once: true }),
    )
    stub.tap({ serialNumber: UID, records: [OLD] })
    expect(await screen.findByText('Backing up the tag…')).toBeInTheDocument()
    ended = new Date(Date.now() - 1000).toISOString()
    await act(() => new Promise((resolve) => setTimeout(resolve, 2500)))
    expect(screen.queryByText('Expired')).toBeNull()
    expect(onAnnounce).not.toHaveBeenCalledWith('Expired. Nothing was bound.')
    await waitFor(() => expect(stub.writes).toHaveLength(1), { timeout: 3000 })
    stub.tap(tapOf(UID, RECORDS))
    expect(await screen.findByText('Written and checked.')).toBeInTheDocument()
  }, 15_000)

  it('says the tag may hold the new records when a begun write ends no_read_back', async () => {
    const job = seedNfcJob({ kind: 'write', records: RECORDS })
    const { user, onAnnounce } = render()
    await user.click(await armButton())
    expect(await screen.findByText('Hold a tag to the phone')).toBeInTheDocument()
    stub.tap({ serialNumber: UID, records: [OLD] })
    await waitFor(() => expect(stub.writes).toHaveLength(1))
    Object.assign(job, { state: 'failed', result: { error: 'no read-back', code: 'no_read_back' } })
    const text = 'No read-back came in time. The tag may hold the new records; its backup can restore it.'
    await waitFor(() => expect(onAnnounce).toHaveBeenLastCalledWith(text), { timeout: 3500 })
    expect(onAnnounce).not.toHaveBeenCalledWith('Expired. Nothing was bound.')
  }, 10_000)

  it('says Expired at 0:00', async () => {
    seedNfcJob({ kind: 'read', ttl_s: 1 })
    render()
    await screen.findByRole('heading', { name: 'Agent wants to read a tag' })
    expect(await screen.findByText('Expired', {}, { timeout: 2500 })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Arm' })).toBeDisabled()
  })
})
