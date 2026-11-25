import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../../api/client'
import { claimNfcJob, createTagBackup, getSpool, listTags, postNfcJobResult, spoolTagRecords, spoolTagWritten } from '../../../api/filament'
import { resetMockState } from '../../handlers'
import { spoolId } from './seed'
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

  it('checks the backup before it looks the spool up', async () => {
    expect(await codeOf(spoolTagRecords(NOBODY, { uid: UID, records: [], backup_id: NOBODY }))).toEqual({ status: 409, code: 'backup_mismatch' })
    expect(await codeOf(spoolTagWritten(NOBODY, { uid: UID, records: [], backup_id: NOBODY, sent: [], read_back: [] } as never))).toEqual({ status: 409, code: 'backup_mismatch' })
    const { backup_id } = await createTagBackup({ uid: UID, records: [], reason: 'spool_write' })
    expect(await codeOf(spoolTagRecords(NOBODY, { uid: UID, records: [], backup_id }))).toEqual({ status: 404, code: undefined })
  })

  it('refuses a backup over 10 minutes old', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    const { backup_id } = await createTagBackup({ uid: UID, records: [], reason: 'spool_write' })
    vi.setSystemTime(Date.now() + 10 * 60 * 1000 + 1000)
    expect(await codeOf(spoolTagRecords(spoolId(1), { uid: UID, records: [], backup_id }))).toEqual({ status: 409, code: 'backup_mismatch' })
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
