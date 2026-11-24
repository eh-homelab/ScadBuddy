import { waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { spoolTagRecords, spoolTagWritten } from '../api/filament'
import { spoolId } from '../mocks/features/filament/seed'
import { server } from '../mocks/server'
import { installNdefStub, type NdefStub, type StubReading } from '../test/ndefStub'
import { writeWithBackup, type WriteStep } from './tagWrite'
import { NfcSession, type WireRecord } from './webNfc'

const SPOOL = spoolId(3)
/** Spool #3's tag in the seed. */
const UID = '04:a1:b2:c3:d4:e5:f6'
const bytesOf = (hex: string) => new Uint8Array(hex.match(/../g)?.map((pair) => parseInt(pair, 16)) ?? [])
/** A tap of a tag that holds these wire records. */
const tapOf = (serialNumber: string, records: WireRecord[]): StubReading => ({
  serialNumber,
  records: records.map((one) => ({ recordType: one.record_type, ...(one.media_type ? { mediaType: one.media_type } : {}), data: bytesOf(one.data_hex) })),
})
/** The stub's last write, as wire records. */
const lastWrite = (stub: NdefStub): WireRecord[] =>
  stub.writes.at(-1)!.map((one) => ({ record_type: one.recordType, media_type: one.mediaType ?? null, data_hex: Array.from(one.data as Uint8Array, (b) => b.toString(16).padStart(2, '0')).join('') }))

/** Each API request, as `METHOD path`, in the order sent. */
function sent() {
  const lines: string[] = []
  const listener = ({ request }: { request: Request }) => void lines.push(`${request.method} ${new URL(request.url).pathname}`)
  server.events.on('request:start', listener)
  return { lines, off: () => server.events.removeListener('request:start', listener) }
}

describe('writeWithBackup (#7273)', () => {
  let stub: NdefStub
  let session: NfcSession
  let steps: WriteStep[]
  beforeEach(async () => {
    stub = installNdefStub()
    session = new NfcSession()
    await session.start()
    steps = []
  })
  afterEach(() => {
    session.stop()
    stub.restore()
    vi.useRealTimers()
  })

  const write = () =>
    writeWithBackup({
      session,
      reason: 'spool_write',
      prepare: (now, backupId) => spoolTagRecords(SPOOL, { uid: now.uid, records: now.records, backup_id: backupId }).then((answer) => answer.records),
      confirm: (now, backupId, records, back) => spoolTagWritten(SPOOL, { uid: now.uid, backup_id: backupId, sent: records, read_back: back.records }),
      onStep: (step) => steps.push(step),
    })

  /** Waits for the stub's write, then holds the tag that answers the read-back. */
  async function readBack(serialNumber: string, records?: WireRecord[]) {
    // `reading_back` comes once the read-back listener is set.
    await waitFor(() => expect(steps).toContain('reading_back'))
    stub.tap(tapOf(serialNumber, records ?? lastWrite(stub)))
  }

  it('backs up before it writes, then checks the read-back', async () => {
    const log = sent()
    try {
      const outcome = write()
      stub.tap({ serialNumber: UID, records: [] })
      await waitFor(() => expect(steps).toContain('writing'))
      const before = [...log.lines]
      await readBack(UID)
      const done = await outcome
      expect(done.status).toBe('checked')
      expect(done.status === 'checked' && done.result?.spool?.number).toBe(3)
      expect(steps).toEqual(['backing_up', 'writing', 'reading_back'])
      // The backup and the records come before the write; the check after it.
      expect(before).toEqual(['POST /api/v1/filament/tags/backups', `POST /api/v1/filament/spools/${SPOOL}/tag-records`])
      expect(log.lines).toEqual([...before, `POST /api/v1/filament/spools/${SPOOL}/tag-written`])
      expect(lastWrite(stub)[0]).toMatchObject({ record_type: 'mime', media_type: 'application/json' })
    } finally {
      log.off()
    }
  })

  it('writes nothing when the backup fails', async () => {
    server.use(http.post('/api/v1/filament/tags/backups', () => HttpResponse.json({ title: 'Internal Server Error', status: 500, detail: 'the store is down' }, { status: 500 })))
    const outcome = write()
    stub.tap({ serialNumber: UID, records: [] })
    const done = await outcome
    expect(done).toMatchObject({ status: 'failed', backupId: null })
    expect(done.status === 'failed' && done.message).toMatch(/the store is down/)
    expect(stub.writes).toEqual([])
  })

  it('writes nothing to a tag with no serial number', async () => {
    const log = sent()
    try {
      const outcome = write()
      stub.tap({ serialNumber: '', records: [] })
      expect(await outcome).toEqual({ status: 'failed', message: 'This tag gives no serial number, so it cannot be backed up. Nothing was written.', backupId: null })
      expect(log.lines).not.toContain('POST /api/v1/filament/tags/backups')
      expect(stub.writes).toEqual([])
    } finally {
      log.off()
    }
  })

  it('says a different tag answered and binds nothing', async () => {
    const log = sent()
    try {
      const outcome = write()
      stub.tap({ serialNumber: UID, records: [] })
      await readBack('04:99:88:77:66:55:44')
      const done = await outcome
      expect(done.status).toBe('other_tag')
      expect(done.status === 'other_tag' && done.backupId).toEqual(expect.any(String))
      expect(log.lines).not.toContain(`POST /api/v1/filament/spools/${SPOOL}/tag-written`)
    } finally {
      log.off()
    }
  })

  it('reports a read-back that differs as a mismatch', async () => {
    const outcome = write()
    stub.tap({ serialNumber: UID, records: [] })
    await readBack(UID, [{ record_type: 'mime', media_type: 'application/json', data_hex: '7b7d' }])
    const done = await outcome
    expect(done.status).toBe('mismatch')
    expect(done.status === 'mismatch' && done.backupId).toEqual(expect.any(String))
  })

  it('reports a read_back_mismatch refusal from the server as a mismatch', async () => {
    server.use(http.post('/api/v1/filament/spools/:id/tag-written', () => HttpResponse.json({ title: 'Conflict', status: 409, detail: 'no', code: 'read_back_mismatch' }, { status: 409 })))
    const outcome = write()
    stub.tap({ serialNumber: UID, records: [] })
    await readBack(UID)
    expect((await outcome).status).toBe('mismatch')
  })

  it('says the tag changed after its backup, and writes nothing', async () => {
    server.use(http.post('/api/v1/filament/spools/:id/tag-records', () => HttpResponse.json({ title: 'Conflict', status: 409, detail: 'the backup does not hold these records', code: 'backup_mismatch' }, { status: 409 })))
    const outcome = write()
    stub.tap({ serialNumber: UID, records: [] })
    const done = await outcome
    expect(done).toMatchObject({ status: 'failed', backupId: expect.any(String) })
    expect(done.status === 'failed' && done.message).toBe('The tag changed after it was backed up, or the backup is too old. Nothing was written. Hold the tag to the phone again.')
    expect(stub.writes).toEqual([])
  })

  it('says the spool changed during the write, and binds nothing', async () => {
    server.use(http.post('/api/v1/filament/spools/:id/tag-written', () => HttpResponse.json({ title: 'Conflict', status: 409, detail: 'sent is not what the spool gives now', code: 'sent_mismatch' }, { status: 409 })))
    const outcome = write()
    stub.tap({ serialNumber: UID, records: [] })
    await readBack(UID)
    const done = await outcome
    expect(done).toMatchObject({ status: 'failed', backupId: expect.any(String) })
    expect(done.status === 'failed' && done.message).toBe('The spool changed while the tag was written, so the tag was not bound. Write the tag again.')
  })

  it('asks for a second tap when no read-back comes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const outcome = write()
    stub.tap({ serialNumber: UID, records: [] })
    await vi.waitFor(() => expect(steps).toContain('reading_back'))
    await vi.advanceTimersByTimeAsync(2_900)
    expect(steps).not.toContain('tap_again')
    await vi.advanceTimersByTimeAsync(200)
    expect(steps.at(-1)).toBe('tap_again')
    stub.tap(tapOf(UID, lastWrite(stub)))
    expect((await outcome).status).toBe('checked')
  })

  it("shows the browser's write error and keeps the backup", async () => {
    stub.failNextWrite(new DOMException('x', 'NetworkError'))
    const outcome = write()
    stub.tap({ serialNumber: UID, records: [] })
    const done = await outcome
    expect(done).toMatchObject({ status: 'failed', backupId: expect.any(String) })
    expect(done.status === 'failed' && done.message).toBe('The tag was not written: x. The tag may be too small for the record, or it moved away too soon.')
  })

  it('writes one empty record to put back a blank tag', async () => {
    // Web NFC refuses a message with no records; a blank tag reads back as none or one empty record.
    const outcome = writeWithBackup({ session, reason: 'restore', prepare: async () => [], confirm: async () => null, onStep: (step) => steps.push(step) })
    stub.tap({ serialNumber: UID, records: [{ recordType: 'text', data: new TextEncoder().encode('hi') }] })
    await waitFor(() => expect(steps).toContain('reading_back'))
    expect(stub.writes[0]).toEqual([{ recordType: 'empty' }])
    stub.tap({ serialNumber: UID, records: [] })
    expect(await outcome).toEqual({ status: 'checked', result: null })
  })

  it('refuses a second write while one runs', async () => {
    const log = sent()
    try {
      const first = write()
      const second = await write()
      expect(second).toEqual({ status: 'failed', message: 'Another write is running. Nothing was written.', backupId: null })
      stub.tap({ serialNumber: UID, records: [] })
      await readBack(UID)
      expect((await first).status).toBe('checked')
      expect(log.lines.filter((line) => line === 'POST /api/v1/filament/tags/backups')).toHaveLength(1)
    } finally {
      log.off()
    }
  })

  it('takes no backup when cancelled after the first tap', async () => {
    const log = sent()
    try {
      const stop = new AbortController()
      const outcome = writeWithBackup({
        session,
        reason: 'spool_write',
        prepare: async () => [],
        confirm: async () => null,
        onStep: (step) => steps.push(step),
        signal: stop.signal,
      })
      stub.tap({ serialNumber: UID, records: [] })
      stop.abort()
      expect(await outcome).toMatchObject({ status: 'failed', backupId: null })
      expect(log.lines).not.toContain('POST /api/v1/filament/tags/backups')
      expect(stub.writes).toEqual([])
    } finally {
      log.off()
    }
  })

  it('keeps the page from seeing the taps of a write', async () => {
    const seen: string[] = []
    session.onReading((reading) => seen.push(reading.uid))
    const outcome = write()
    stub.tap({ serialNumber: UID, records: [] })
    await readBack(UID)
    await outcome
    expect(seen).toEqual([])
    stub.tap({ serialNumber: UID, records: [] })
    expect(seen).toEqual([UID])
  })
})
