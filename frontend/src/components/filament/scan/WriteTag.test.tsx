import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { postTagRead, type TagReadResult } from '../../../api/filament'
import { server } from '../../../mocks/server'
import { NfcSession, type WireRecord } from '../../../lib/webNfc'
import { installNdefStub, type NdefStub, type StubReading } from '../../../test/ndefStub'
import { renderPage } from '../../../test/utils'
import { WriteTag } from './WriteTag'

/** Spool #3's tag in the seed. */
const UID = '04:a1:b2:c3:d4:e5:f6'
const TEXT = { recordType: 'text', data: new TextEncoder().encode('hello') }
const bytesOf = (hex: string) => new Uint8Array(hex.match(/../g)?.map((pair) => parseInt(pair, 16)) ?? [])
const tapOf = (serialNumber: string, records: WireRecord[]): StubReading => ({
  serialNumber,
  records: records.map((one) => ({ recordType: one.record_type, ...(one.media_type ? { mediaType: one.media_type } : {}), data: bytesOf(one.data_hex) })),
})
const lastWrite = (stub: NdefStub): WireRecord[] =>
  stub.writes.at(-1)!.map((one) => ({ record_type: one.recordType, media_type: one.mediaType ?? null, data_hex: Array.from((one.data as Uint8Array | undefined) ?? [], (b) => b.toString(16).padStart(2, '0')).join('') }))

describe('WriteTag (#7273)', () => {
  let stub: NdefStub
  let session: NfcSession
  let known: TagReadResult
  beforeEach(async () => {
    stub = installNdefStub()
    session = new NfcSession()
    await session.start()
    known = await postTagRead({ uid: UID, records: [] })
  })
  afterEach(() => {
    session.stop()
    stub.restore()
  })

  const render = (props: Partial<Parameters<typeof WriteTag>[0]> = {}) => renderPage(<WriteTag session={session} spool={known.spool!} tag={known.tag!} onClose={() => {}} {...props} />)
  const stepNamed = (name: string) => within(screen.getByRole('list', { name: 'Steps' })).getByText(name).closest('li')!
  /** Waits until the flow listens for the first tap. */
  const listening = () => screen.findByText('Hold the tag still. If asked, lift it and tap again.')
  /** Waits until the page listens for the read-back, then holds the tag that answers it. */
  async function readBack(serialNumber: string, records?: WireRecord[]) {
    await waitFor(() => expect(stepNamed('Read back')).toHaveAttribute('aria-current', 'step'))
    stub.tap(tapOf(serialNumber, records ?? lastWrite(stub)))
  }

  it('walks the three steps on one tap and says written and checked', async () => {
    const onWritten = vi.fn()
    render({ onWritten })
    expect(stepNamed('Read and back up')).toHaveAttribute('aria-current', 'step')
    await listening()
    stub.tap({ serialNumber: UID, records: [TEXT] })
    await readBack(UID)
    expect(await screen.findByText('Written and checked.')).toBeInTheDocument()
    for (const name of ['Read and back up', 'Write', 'Read back']) expect(stepNamed(name)).toHaveTextContent('Done')
    expect(screen.getByText('Written as OpenSpool')).toBeInTheDocument()
    await waitFor(() => expect(onWritten).toHaveBeenCalledTimes(1))
    expect(onWritten.mock.calls[0]![0]).toBe(UID)
    expect((onWritten.mock.calls[0]![2] as TagReadResult).spool?.number).toBe(3)
  })

  it('asks to lift and tap again when the read-back does not come', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      render()
      await listening()
      stub.tap({ serialNumber: UID, records: [TEXT] })
      await waitFor(() => expect(stepNamed('Read back')).toHaveAttribute('aria-current', 'step'))
      await vi.advanceTimersByTimeAsync(3_100)
      expect(await screen.findByText('Lift the tag and tap it again to check it.')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('offers restore after a mismatch', async () => {
    const { user } = render()
    await listening()
    stub.tap({ serialNumber: UID, records: [TEXT] })
    await readBack(UID, [{ record_type: 'mime', media_type: 'application/json', data_hex: '7b7d' }])
    expect(await screen.findByText('The tag does not hold what was sent.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Restore from the backup' }))
    await listening()
    stub.tap({ serialNumber: UID, records: [{ recordType: 'mime', mediaType: 'application/json', data: bytesOf('7b7d') }] })
    await waitFor(() => expect(stub.writes).toHaveLength(2))
    // The backup taken before the first write: the text record.
    expect(lastWrite(stub)).toEqual([{ record_type: 'text', media_type: null, data_hex: '68656c6c6f' }])
    await readBack(UID)
    expect(await screen.findByText('Restored and checked.')).toBeInTheDocument()
  })

  it('shows the missing fields the format named', async () => {
    server.use(
      http.post('/api/v1/filament/spools/:id/tag-records', () =>
        HttpResponse.json({ format: 'openspool', records: [{ record_type: 'mime', media_type: 'application/json', data_hex: '7b7d' }], warnings: ['density', 'weight'] }),
      ),
    )
    render()
    await listening()
    stub.tap({ serialNumber: UID, records: [TEXT] })
    expect(await screen.findByText('Missing on the spool: density, weight')).toBeInTheDocument()
    expect(screen.getByText(/as OpenSpool$/)).toBeInTheDocument()
  })

  it('says the tag may be too small when the browser refuses the write', async () => {
    stub.failNextWrite(new DOMException('NFC write failed', 'NetworkError'))
    render()
    await listening()
    stub.tap({ serialNumber: UID, records: [TEXT] })
    expect(await screen.findByText('The tag was not written: NFC write failed. The tag may be too small for the record, or it moved away too soon.')).toBeInTheDocument()
    expect(screen.getByText('The backup is kept.')).toBeInTheDocument()
  })

  it('says a different tag answered', async () => {
    render()
    await listening()
    stub.tap({ serialNumber: UID, records: [TEXT] })
    await readBack('04:99:88:77:66:55:44')
    expect(await screen.findByText('A different tag answered. Nothing was bound.')).toBeInTheDocument()
  })

  it('says it runs only while it holds the session', async () => {
    const onRunning = vi.fn()
    render({ onRunning })
    await listening()
    await waitFor(() => expect(onRunning).toHaveBeenLastCalledWith(true))
    stub.tap({ serialNumber: UID, records: [TEXT] })
    await readBack(UID)
    expect(await screen.findByText('Written and checked.')).toBeInTheDocument()
    expect(onRunning.mock.calls).toEqual([[true], [false]])
  })

  it('says nothing of running when another run holds the session', async () => {
    // The other run (an agent job, another flow) is still under way: this flow must not tell
    // the parent that runs ended.
    const release = session.claim()!
    try {
      const onRunning = vi.fn()
      render({ onRunning })
      expect(await screen.findByText('Another write is running. Nothing was written.')).toBeInTheDocument()
      expect(onRunning).not.toHaveBeenCalled()
    } finally {
      release()
    }
  })
})
