import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../../api/client'
import { claimNfcJob, createTagBackup, getSpool, listTags, postNfcJobResult, postTagRead, spoolTagRecords, spoolTagWritten } from '../../../api/filament'
import { resetMockState } from '../../handlers'
import { L, spoolId } from './seed'
import { seedNfcJob } from './tags'

// #7273 — the tag mocks answer as the real routes do (`backend/scadbuddy/api/filament.py`, `filament/tags.py`).

const NOBODY = '00000000-0000-4000-8000-000000000000'
const UID = '04:0B:0B:0B:0B:0B:0B'

const codeOf = (call: Promise<unknown>) =>
  call.then(
    () => null,
    (cause: unknown) => (cause instanceof ApiError ? { status: cause.status, code: cause.problem.code } : cause),
  )

describe('the tag mocks (#7273)', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.useRealTimers())

  it("ends a read job with the reading's answer, as POST /tags/reads gives it, and marks the tag seen by the agent", async () => {
    const job = seedNfcJob({ kind: 'read' })
    await claimNfcJob(job.id)
    const done = await postNfcJobResult(job.id, { uid: '04:a1:b2:c3:d4:e5:f6', records: [] })
    expect(done.state).toBe('done')
    expect(done.result).toMatchObject({ next: 'open_spool', spool: { number: 3 }, decoded: null, draft: null, uid: '04A1B2C3D4E5F6', records: [] })
    expect(done.result).toHaveProperty('tag.last_seen_source', 'agent')
    const tag = (await listTags()).find((one) => one.tag_uid === '04A1B2C3D4E5F6')!
    expect(tag.last_seen_source).toBe('agent')
  })

  it("keeps a read's chip on the tag, and tag-records warn when the tag is too small (#8366)", async () => {
    const read = await postTagRead({ uid: '04:0d:0d:0d', tag_type: null, records: [], chip: 'NTAG213', max_ndef_bytes: 40, writable: true })
    expect(read.tag).toMatchObject({ chip: 'NTAG213', max_ndef_bytes: 40, writable: true })
    const again = await postTagRead({ uid: '04:0d:0d:0d', tag_type: null, records: [] })
    expect(again.tag).toMatchObject({ chip: 'NTAG213', max_ndef_bytes: 40 })
    const { backup_id } = await createTagBackup({ uid: '04:0d:0d:0d', records: [], reason: 'spool_write' })
    const out = await spoolTagRecords(spoolId(3), { uid: '04:0d:0d:0d', records: [], backup_id })
    expect((out.warnings ?? []).some((one) => one.includes('this NTAG213 holds 40'))).toBe(true)
  })

  // The hand-held Scan mode (#6955): `tag_codes.resolve_records` over the seed's places and spools.
  const hex = (text: string) => Array.from(new TextEncoder().encode(text), (byte) => byte.toString(16).padStart(2, '0')).join('')
  const url = (href: string) => ({ record_type: 'url', data_hex: hex(href) })
  const text = (value: string) => ({ record_type: 'text', encoding: 'utf-8', data_hex: hex(value) })

  it('a read with a place-code URL answers open_code and the place', async () => {
    const got = await postTagRead({ uid: '04:0c:0c:0c:0c:0c:0c', tag_type: null, records: [url('https://scadbuddy.example/x/L-4?from=label')] })
    expect(got.next).toBe('open_code')
    expect(got.resolved).toEqual({ kind: 'location', id: L.AMS_A1, code: 'L-4', name: expect.any(String), archived: false })
    expect(got.tag?.tag_uid).toBe('040C0C0C0C0C0C')
    expect(got.spool).toBeNull()
  })

  it('a read with a text S-<n> answers the spool', async () => {
    const got = await postTagRead({ uid: null, tag_type: null, records: [text('s3')] })
    expect(got.next).toBe('open_code')
    expect(got.resolved).toEqual({ kind: 'spool', id: spoolId(3), code: 'S-3', name: null, archived: false })
    expect(got.tag).toBeNull()
  })

  it('a scadbuddy:spool text answers the spool', async () => {
    const got = await postTagRead({ uid: null, tag_type: null, records: [text(`scadbuddy:spool:${spoolId(3)}`)] })
    expect(got.next).toBe('open_code')
    expect(got.resolved).toMatchObject({ kind: 'spool', id: spoolId(3), code: 'S-3' })
  })

  it('a bound tag ignores a code in its records', async () => {
    const got = await postTagRead({ uid: '04:a1:b2:c3:d4:e5:f6', tag_type: null, records: [url('https://h/x/L-4')] })
    expect(got.next).toBe('open_spool')
    expect(got.spool?.number).toBe(3)
    expect(got.resolved ?? null).toBeNull()
  })

  it('an unknown code is a new tag', async () => {
    const got = await postTagRead({ uid: '04:0c:0c:0c:0c:0c:0c', tag_type: null, records: [text('L-999999')] })
    expect(got.next).toBe('bind_or_create')
    expect(got.resolved ?? null).toBeNull()
  })

  it('stops at the first record that is a code, as `tag_codes.code_in` does, even when it names nothing', async () => {
    const got = await postTagRead({ uid: '04:0c:0c:0c:0c:0c:0c', tag_type: null, records: [text('L-999999'), url('https://h/x/L-4')] })
    expect(got.next).toBe('bind_or_create')
    expect(got.resolved ?? null).toBeNull()
  })

  it('a record that is no code is skipped for a later one that is', async () => {
    const got = await postTagRead({ uid: null, tag_type: null, records: [text('hello'), url('https://h/x/L-4')] })
    expect(got.next).toBe('open_code')
    expect(got.resolved).toMatchObject({ kind: 'location', code: 'L-4' })
  })

  it('looks the spool up before it checks the backup', async () => {
    expect(await codeOf(spoolTagRecords(NOBODY, { uid: UID, records: [], backup_id: NOBODY }))).toEqual({ status: 404, code: undefined })
    expect(await codeOf(spoolTagRecords(spoolId(1), { uid: UID, records: [], backup_id: NOBODY }))).toEqual({ status: 409, code: 'backup_mismatch' })
    expect(await codeOf(spoolTagWritten(NOBODY, { uid: UID, records: [], backup_id: NOBODY, sent: [], read_back: [] } as never))).toEqual({ status: 404, code: undefined })
    expect(await codeOf(spoolTagWritten(spoolId(1), { uid: UID, records: [], backup_id: NOBODY, sent: [], read_back: [] } as never))).toEqual({ status: 409, code: 'backup_mismatch' })
    const { backup_id } = await createTagBackup({ uid: UID, records: [], reason: 'spool_write' })
    expect(await codeOf(spoolTagRecords(NOBODY, { uid: UID, records: [], backup_id }))).toEqual({ status: 404, code: undefined })
  })

  it('refuses a backup over 10 minutes old', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    const { backup_id } = await createTagBackup({ uid: UID, records: [], reason: 'spool_write' })
    vi.setSystemTime(Date.now() + 10 * 60 * 1000 + 1000)
    expect(await codeOf(spoolTagRecords(spoolId(1), { uid: UID, records: [], backup_id }))).toEqual({ status: 409, code: 'backup_mismatch' })
  })

  // #8364: `_openspool_encode` writes the extended keys after the published 7, in a fixed order.
  it("writes the spool's extended OpenSpool keys after the published ones, and reads them back", async () => {
    const { backup_id } = await createTagBackup({ uid: UID, records: [], reason: 'spool_write' })
    const { records } = await spoolTagRecords(spoolId(1), { uid: UID, records: [], backup_id })
    const json = new TextDecoder().decode(Uint8Array.from(records[0]!.data_hex.match(/../g)!, (byte) => parseInt(byte, 16)))
    const keys = Object.keys(JSON.parse(json) as object)
    const order = ['protocol', 'version', 'type', 'color_hex', 'brand', 'min_temp', 'max_temp', 'subtype', 'bed_min_temp', 'bed_max_temp', 'alpha', 'additional_color_hexes', 'weight', 'diameter']
    expect(keys).toEqual(order.filter((key) => keys.includes(key)))
    const spool = await getSpool(spoolId(1))
    if (spool.effective?.label_weight_g != null) expect(keys).toContain('weight')
    if (spool.effective?.diameter_mm != null) expect(keys).toContain('diameter')
    const read = await postTagRead({ uid: '04:0d:0d:0d:0d:0d:0d', tag_type: null, records })
    expect(read.decoded).toMatchObject({ format: 'openspool', label_weight_g: spool.effective?.label_weight_g ?? null })
  })

  it('reads an OpenSpool tag\'s unknown keys as their JSON text', async () => {
    const tag = { protocol: 'openspool', version: '1.0', type: 'PLA', weight: '1000', diameter: '1.75', lot: 7, note: '<b>hi</b>' }
    const read = await postTagRead({ uid: '04:0e:0e:0e:0e:0e:0e', tag_type: null, records: [{ record_type: 'mime', media_type: 'application/json', data_hex: hex(JSON.stringify(tag)) }] })
    expect(read.decoded).toMatchObject({ label_weight_g: 1000, diameter_mm: '1.75', other_keys: { lot: '7', note: '"<b>hi</b>"' } })
  })

  it("gives a checked write the format's origin and the write's time", async () => {
    const { backup_id } = await createTagBackup({ uid: UID, records: [], reason: 'spool_write' })
    const { records } = await spoolTagRecords(spoolId(1), { uid: UID, records: [], backup_id })
    const before = Date.now()
    const result = await spoolTagWritten(spoolId(1), { uid: UID, backup_id, sent: records, read_back: records })
    expect(result.spool).toMatchObject({ data_origin: 'openspool' })
    const spool = await getSpool(spoolId(1))
    expect(spool.data_origin).toBe('openspool')
    expect(Date.parse(spool.tag_encoded_at!)).toBeGreaterThanOrEqual(before - 1000)
  })
})
