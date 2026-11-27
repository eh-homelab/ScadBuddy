import { waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { getLocation, postTagRead, type LocationDetail, type TagReadResult } from '../api/filament'
import { L } from '../mocks/features/filament/seed'
import { server } from '../mocks/server'
import { installNdefStub, type NdefStub } from '../test/ndefStub'
import { placeRecords, writePlaceTag } from './placeTag'
import type { WriteStep } from './tagWrite'
import { NfcSession } from './webNfc'

const BASE = 'https://sb.example'
/** A tag no spool holds. */
const FREE = '04:77:66:55:44:33:22'
/** Spool #3's tag in the seed. */
const HELD = '04:a1:b2:c3:d4:e5:f6'
const textOf = (hex: string) => new TextDecoder().decode(new Uint8Array(hex.match(/../g)!.map((pair) => parseInt(pair, 16))))
const lastWrite = (stub: NdefStub) => stub.writes.at(-1)!
const urlTap = (serialNumber: string, url: string) => ({ serialNumber, records: [{ recordType: 'url', data: new TextEncoder().encode(url) }] })

describe('placeRecords (#7168 R6)', () => {
  it('is one url record that holds the URL as UTF-8', () => {
    const records = placeRecords('https://sb.example/x/L-4')
    expect(records).toHaveLength(1)
    expect(records[0]!.record_type).toBe('url')
    expect(textOf(records[0]!.data_hex)).toBe('https://sb.example/x/L-4')
  })
})

describe('writePlaceTag (#7168 R6)', () => {
  let stub: NdefStub
  let session: NfcSession
  let steps: WriteStep[]
  let place: LocationDetail
  let readOnly: MockInstance
  beforeEach(async () => {
    stub = installNdefStub()
    readOnly = vi.spyOn((window as unknown as { NDEFReader: { prototype: { makeReadOnly: () => Promise<void> } } }).NDEFReader.prototype, 'makeReadOnly')
    session = new NfcSession()
    await session.start()
    steps = []
    place = await getLocation(L.SHELF_A)
  })
  afterEach(() => {
    // A place tag stays reusable: nothing ever locks it.
    expect(readOnly).toHaveBeenCalledTimes(0)
    session.stop()
    stub.restore()
  })

  const write = () => writePlaceTag(session, place, BASE, { onStep: (step) => steps.push(step), onRecords: () => {}, onClaimed: () => {}, signal: new AbortController().signal })
  const backups = () => {
    const bodies: { reason: string }[] = []
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname.endsWith('/tags/backups') && request.method === 'POST') void request.clone().json().then((body) => bodies.push(body as { reason: string }))
    })
    return bodies
  }

  it('backs the tag up as a place write, writes the label URL and checks the read-back names this place', async () => {
    const sent = backups()
    const outcome = write()
    stub.tap({ serialNumber: FREE, records: [] })
    await waitFor(() => expect(steps).toContain('reading_back'))
    const url = `${BASE}/x/${place.code}`
    expect(lastWrite(stub)).toEqual([{ recordType: 'url', data: url }])
    stub.tap(urlTap(FREE, url))
    const done = await outcome
    expect(done.status).toBe('checked')
    expect(done.status === 'checked' && done.result?.next).toBe('open_code')
    expect(done.status === 'checked' && done.result?.resolved?.id).toBe(place.id)
    await waitFor(() => expect(sent).toEqual([{ uid: FREE, records: [], reason: 'place_write' }]))
  })

  it('refuses a tag that a spool holds and writes nothing', async () => {
    const held = await postTagRead({ uid: HELD, records: [] })
    server.use(http.post('/api/v1/filament/tags/reads', () => HttpResponse.json({ ...held, spool: { ...held.spool!, number: 12 } } satisfies TagReadResult)))
    const outcome = write()
    stub.tap({ serialNumber: HELD, records: [] })
    const done = await outcome
    expect(done.status).toBe('failed')
    expect(done.status === 'failed' && done.message).toContain('Unbind it from S-12 first.')
    expect(stub.writes).toHaveLength(0)
  })

  it('fails when the read-back names another place', async () => {
    const other = await getLocation(L.DRY_BOX)
    const outcome = write()
    stub.tap({ serialNumber: FREE, records: [] })
    await waitFor(() => expect(steps).toContain('reading_back'))
    const url = `${BASE}/x/${place.code}`
    // The tag holds what was sent, but the server reads it as another place.
    server.use(http.post('/api/v1/filament/tags/reads', () => HttpResponse.json({ tag: null, spool: null, decoded: null, draft: null, next: 'open_code', resolved: { kind: 'location', id: other.id, code: other.code, name: other.name, archived: false } } satisfies TagReadResult)))
    stub.tap(urlTap(FREE, url))
    const done = await outcome
    expect(done.status).toBe('failed')
    expect(done.status === 'failed' && done.message).toContain(other.code)
    expect(done.status === 'failed' && done.backupId).toBeTruthy()
  })
})
