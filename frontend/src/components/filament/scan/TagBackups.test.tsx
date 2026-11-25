import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createTagBackup } from '../../../api/filament'
import { TAG } from '../../../mocks/features/filament/seed'
import { server } from '../../../mocks/server'
import { NfcSession, type WireRecord } from '../../../lib/webNfc'
import { installNdefStub, type NdefStub } from '../../../test/ndefStub'
import { renderPage } from '../../../test/utils'
import { TagBackups } from './TagBackups'

/** Spool #3's tag in the seed (`TAG.MOVED`). */
const UID = '04:a1:b2:c3:d4:e5:f6'
const OLD: WireRecord[] = [{ record_type: 'text', media_type: null, data_hex: '68656c6c6f' }]
const NOW: WireRecord[] = [{ record_type: 'mime', media_type: 'application/json', data_hex: '7b7d' }]

/** Each API request, as `METHOD path` with its JSON body, in the order sent. */
function sent() {
  const requests: { line: string; body: unknown }[] = []
  const listener = ({ request }: { request: Request }) => {
    const entry: { line: string; body: unknown } = { line: `${request.method} ${new URL(request.url).pathname}`, body: null }
    requests.push(entry)
    void request
      .clone()
      .text()
      .then((text) => (entry.body = text ? (JSON.parse(text) as unknown) : null))
  }
  server.events.on('request:start', listener)
  return { requests, off: () => server.events.removeListener('request:start', listener) }
}

describe('TagBackups (#7273)', () => {
  let stub: NdefStub
  let session: NfcSession
  beforeEach(async () => {
    stub = installNdefStub()
    session = new NfcSession()
    await session.start()
  })
  afterEach(() => {
    session.stop()
    stub.restore()
  })

  it('lists backups newest first with their reason', async () => {
    await createTagBackup({ uid: UID, records: OLD, reason: 'spool_write' })
    await createTagBackup({ uid: UID, records: [...OLD, ...NOW], reason: 'restore' })
    const { user } = renderPage(<TagBackups session={session} tagId={TAG.MOVED} />)
    await user.click(await screen.findByRole('button', { name: 'Backups (2)' }))
    const rows = within(screen.getByRole('list', { name: 'Backups' })).getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('before a restore')
    expect(rows[0]).toHaveTextContent('2 records')
    expect(rows[1]).toHaveTextContent('before a spool write')
    expect(rows[1]).toHaveTextContent('1 record')
    expect(rows[1]).toHaveTextContent('owner@example.com')
  })

  it('says 50+ when the first page is full', async () => {
    for (let n = 0; n < 50; n += 1) await createTagBackup({ uid: UID, records: OLD, reason: 'spool_write' })
    renderPage(<TagBackups session={session} tagId={TAG.MOVED} />)
    expect(await screen.findByRole('button', { name: 'Backups (50+)' })).toBeInTheDocument()
  })

  it('restores a backup with a fresh backup first', async () => {
    await createTagBackup({ uid: UID, records: OLD, reason: 'spool_write' })
    const log = sent()
    try {
      const { user } = renderPage(<TagBackups session={session} tagId={TAG.MOVED} />)
      await user.click(await screen.findByRole('button', { name: 'Backups (1)' }))
      await user.click(screen.getByRole('button', { name: 'Restore' }))
      await screen.findByText('Hold the tag still. If asked, lift it and tap again.')
      stub.tap({ serialNumber: UID, records: [{ recordType: 'mime', mediaType: 'application/json', data: new Uint8Array([0x7b, 0x7d]) }] })
      await waitFor(() => expect(stub.writes).toHaveLength(1))
      const lines = log.requests.map((one) => one.line)
      const backup = lines.indexOf('POST /api/v1/filament/tags/backups')
      const restore = lines.findIndex((line) => /^POST \/api\/v1\/filament\/tags\/[^/]+\/backups\/[^/]+\/restore$/.test(line))
      expect(backup).toBeGreaterThan(-1)
      expect(restore).toBeGreaterThan(backup)
      expect(log.requests[backup]!.body).toMatchObject({ reason: 'restore', records: NOW })
      // The old records go back on the tag.
      expect(stub.writes[0]!.map((one) => ({ ...one, data: [...(one.data as Uint8Array)] }))).toEqual([{ recordType: 'text', data: [...new TextEncoder().encode('hello')] }])
      const readBack = within(screen.getByRole('list', { name: 'Steps' })).getByText('Read back').closest('li')!
      await waitFor(() => expect(readBack).toHaveAttribute('aria-current', 'step'))
      stub.tap({ serialNumber: UID, records: [{ recordType: 'text', data: new TextEncoder().encode('hello') }] })
      expect(await screen.findByText('Restored and checked.')).toBeInTheDocument()
      await waitFor(() => expect(log.requests.map((one) => one.line)).toContain('POST /api/v1/filament/tags/reads'))
      // The list shows the fresh backup too.
      expect(await screen.findByRole('button', { name: 'Backups (2)' })).toBeInTheDocument()
    } finally {
      log.off()
    }
  })
})
