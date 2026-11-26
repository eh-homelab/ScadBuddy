import { HttpResponse, http } from 'msw'
import type { components } from '../../../api/schema'
import { parseRgba } from '../../../lib/rgba'
// `same_records`'s rule is the page's own `sameRecords`.
import { recordsSha256, sameRecords } from '../../../lib/webNfc'
import { newId } from '../../newId'
import { bindOn, cleanTagUid, findByKey, holderOf, parseCode, resolveCodeText, resolvedSpool, seenTag, setOrigin, spoolRowOf, spoolViewOf, tagOf, tagRowOf } from './index'

type S = components['schemas']

/**
 * #7273 — `POST /api/v1/filament/tags/reads` (`backend/scadbuddy/filament/tags.py` `read_on`)
 * over the seeded tags, which `index.ts` owns. A read with a UID finds or makes its tag; with
 * `bind_spool_id` it binds it there, through the tag route's own `bindOn`. `next` is
 * `open_spool` when a spool holds the tag; `open_code`, with `resolved`, when no spool holds it
 * and its records name a row (`tag_codes.py`: a `url` record whose path ends `/x/<code>`, a
 * `text` record that is a code or `scadbuddy:spool:<id>`; the first record that parses as one
 * decides, so a code no row has is `bind_or_create`, never a later record's row);
 * `bind_or_create` for a tag no spool holds, and `none` with no UID. Only OpenSpool's JSON is
 * decoded here; an OpenTag3D record reads as no format.
 *
 * The write routes (`back_up`, `records_for_spool`, `written`, `restore_records`) keep each
 * tag's backups in memory, append-only; a backup covers a write for 10 minutes. `tag-records`
 * answers one OpenSpool record built from the spool; `tag-written` checks the backup, the
 * read-back and the records, then binds and gives the spool the `openspool` origin and the
 * write's time. Both look the spool up first (404), then check the backup, as the routes do.
 *
 * The agents' NFC jobs (`tags.py` `claim_job`, `finish_job`, `cancel_job`) live in memory too;
 * `seedNfcJob` queues one. A job past its `expires_at` is `expired` at the next request, but for an
 * armed one whose `agent_write` backup exists (`write_begun`), which no cancel ends and which fails
 * `no_read_back` once its newest backup is over 10 minutes old; arming sets `expires_at`
 * `ARM_SECONDS` ahead. A read
 * job's result is the reading's answer, as `/tags/reads` gives it, with the tag marked seen by
 * the agent.
 */

const BASE = '/api/v1/filament'
const OPENSPOOL_MEDIA = 'application/json'

const text = (value: unknown) => (typeof value === 'string' && value.trim() ? value.trim() : null)

function temp(value: unknown): number | null {
  const number = typeof value === 'string' && /^\d{1,4}$/.test(value.trim()) ? Number(value) : null
  return number && number > 0 ? number : null
}

function utf8(hex: string): string | null {
  if (!/^([0-9a-f]{2})*$/i.test(hex)) return null
  const bytes = new Uint8Array(hex.length / 2)
  for (let at = 0; at < bytes.length; at += 1) bytes[at] = parseInt(hex.slice(at * 2, at * 2 + 2), 16)
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    return null
  }
}

/** `tag_formats.decode` for OpenSpool: the first `application/json` record whose `protocol` is `openspool`. */
export function decodeOpenSpool(records: readonly S['NdefRecord'][]): S['DecodedSpool'] | null {
  for (const record of records) {
    if ((record.media_type ?? '').toLowerCase() !== OPENSPOOL_MEDIA) continue
    const json = utf8(record.data_hex)
    if (json === null) continue
    let obj: unknown
    try {
      obj = JSON.parse(json)
    } catch {
      continue
    }
    if (!obj || typeof obj !== 'object' || (obj as { protocol?: unknown }).protocol !== 'openspool') continue
    const tag = obj as Record<string, unknown>
    const hex = text(tag.color_hex)
    return {
      format: 'openspool',
      version: text(tag.version) ?? '',
      material: text(tag.type),
      brand: text(tag.brand),
      rgba: hex ? parseRgba(hex.startsWith('#') ? hex : `#${hex}`) : null,
      nozzle_temp_min: temp(tag.min_temp),
      nozzle_temp_max: temp(tag.max_temp),
      warnings: [],
    }
  }
  return null
}

/** `draft_of`: a new spool as the tag describes it, its material and brand found by key; none without a material. */
function draftOf(decoded: S['DecodedSpool']): S['SpoolCreate-Output'] | null {
  const material = decoded.material ? findByKey('materials', decoded.material) : null
  if (!material) return null
  return {
    material_id: material,
    brand_id: decoded.brand ? findByKey('brands', decoded.brand) : null,
    subtype_id: decoded.subtype ? findByKey('subtypes', decoded.subtype, material) : null,
    rgba: decoded.rgba ?? null,
    nozzle_temp_min: decoded.nozzle_temp_min ?? null,
    nozzle_temp_max: decoded.nozzle_temp_max ?? null,
    weight_locked: false,
  }
}

const problem = (status: number, detail: string, code?: string) =>
  HttpResponse.json({ type: 'about:blank', title: status === 404 ? 'Not Found' : 'Unprocessable Content', status, detail, ...(code ? { code } : {}) }, { status, headers: { 'Content-Type': 'application/problem+json' } })

type Backup = S['TagBackup']

/** Every backup, oldest first. */
let backups: Backup[] = []

type Job = S['NfcJob']

/** Every NFC job, oldest first. */
let jobs: Job[] = []

export function reset() {
  backups = []
  jobs = []
}

/** Queues an agent's job: a `read`, or a `write` of `records`; it waits `ttl_s` seconds (default 600). */
export function seedNfcJob(job: { kind: Job['kind']; records?: S['NdefRecord'][]; ttl_s?: number }): Job {
  const created = new Date()
  const made: Job = {
    id: newId(),
    kind: job.kind,
    records: job.kind === 'write' ? (job.records ?? []).map((one) => ({ media_type: null, id: null, encoding: null, lang: null, ...one, data_hex: one.data_hex.toLowerCase() })) : null,
    created_by: 'agent@example.com',
    state: 'pending',
    result: null,
    created_at: created.toISOString(),
    expires_at: new Date(created.getTime() + (job.ttl_s ?? 600) * 1000).toISOString(),
    armed_by: null,
    write_begun: false,
  }
  jobs.push(made)
  return made
}

/** `ARM_SECONDS`: how long an armed run has. */
const ARM_MS = 60_000

/** `expire_jobs`: every open job whose time is up ends as `expired`, but a begun write, which fails `no_read_back` once its newest backup is too old. */
function expireJobs() {
  const now = Date.now()
  for (const job of jobs) {
    if ((job.state !== 'pending' && job.state !== 'armed') || Date.parse(job.expires_at) > now) continue
    const newest = backups.filter((one) => one.job_id === job.id && one.reason === 'agent_write').at(-1)
    if (!newest) job.state = 'expired'
    else if (job.state === 'armed' && now - Date.parse(newest.at) > BACKUP_TTL_MS)
      Object.assign(job, { state: 'failed', result: { error: 'no read-back came within 10 minutes of the backup', code: 'no_read_back', backup_id: newest.id } })
  }
}

const jobOf = (id: string) => {
  expireJobs()
  return jobs.find((one) => one.id === id)
}

const hexOfText = (value: string) => Array.from(new TextEncoder().encode(value), (byte) => byte.toString(16).padStart(2, '0')).join('')

/** `_openspool_encode` for the spool as its routes answer it. */
function openSpoolRecords(spool: NonNullable<ReturnType<typeof spoolViewOf>>): S['TagRecords'] {
  const obj: Record<string, string> = { protocol: 'openspool', version: '1.0' }
  const warnings: string[] = []
  if (spool.names?.material) obj.type = spool.names.material
  else warnings.push('type')
  const rgba = spool.effective?.rgba ?? spool.rgba
  if (rgba) obj.color_hex = rgba.replace('#', '').slice(0, 6).toUpperCase()
  obj.brand = spool.names?.brand ?? 'Generic'
  const min = spool.effective?.nozzle_temp_min
  const max = spool.effective?.nozzle_temp_max
  if (min != null) obj.min_temp = String(min)
  if (max != null) obj.max_temp = String(max)
  return { format: 'openspool', records: [{ record_type: 'mime', media_type: OPENSPOOL_MEDIA, data_hex: hexOfText(JSON.stringify(obj)) }], warnings }
}

const conflict = (code: string, detail: string) =>
  HttpResponse.json({ type: 'about:blank', title: 'Conflict', status: 409, detail, code }, { status: 409, headers: { 'Content-Type': 'application/problem+json' } })

/** `BACKUP_TTL`: a backup covers a write for 10 minutes. */
const BACKUP_TTL_MS = 10 * 60 * 1000

/** `valid_backup`: the backup of this write, of the tag as just read, with this reason, at most 10 minutes old. */
function covering(body: { uid: string; backup_id: string; records?: S['NdefRecord'][] }, reason: Backup['reason']): Backup | Response {
  const backup = backups.find((one) => one.id === body.backup_id)
  if (
    !backup ||
    backup.reason !== reason ||
    cleanTagUid(backup.uid) !== cleanTagUid(body.uid) ||
    Date.now() - Date.parse(backup.at) > BACKUP_TTL_MS ||
    (body.records && !sameRecords(backup.records, body.records))
  ) {
    return conflict('backup_mismatch', 'the backup does not cover this write')
  }
  return backup
}

/** What `tag_codes.code_in` finds in a reading's records: a typed code's text, or a spool id. */
type CodeFound = { code: string } | { spoolId: string }

const SPOOL_TEXT = /^scadbuddy:spool:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i

/**
 * `tag_codes.code_in`: the first record that parses as a code (a `url` whose path ends `x/<code>`,
 * a `text` that is a bare code) or as a `scadbuddy:spool:<id>` text decides, whether or not it
 * names a row; a later record is never read. Text records are read as UTF-8 only: the real route
 * also reads UTF-16 (`tag_codes.decode_text`), which no mock case needs.
 */
function codeIn(records: readonly S['NdefRecord'][]): CodeFound | null {
  for (const record of records) {
    const decoded = utf8(record.data_hex)
    if (decoded === null) continue
    if (record.record_type === 'url') {
      let path: string
      try {
        path = new URL(decoded).pathname
      } catch {
        continue
      }
      const segments = path.split('/').filter(Boolean)
      if (segments.length < 2 || segments[segments.length - 2] !== 'x') continue
      const last = segments[segments.length - 1]!
      if (parseCode(last)) return { code: last }
    } else if (record.record_type === 'text') {
      const trimmed = decoded.trim()
      if (parseCode(trimmed)) return { code: trimmed }
      const spool = SPOOL_TEXT.exec(trimmed)
      if (spool) return { spoolId: spool[1]!.toLowerCase() }
    }
  }
  return null
}

/** `tag_codes.resolve_records`: the row the found code or spool id names, or null (`code_in`'s rule above). */
function resolveRecords(records: readonly S['NdefRecord'][]): S['Resolved'] | null {
  const found = codeIn(records)
  if (!found) return null
  if ('code' in found) return resolveCodeText(found.code)
  const row = spoolRowOf(found.spoolId)
  return row ? resolvedSpool(row) : null
}

/** `read_on`: a reading's answer. A read with a UID finds or makes its tag, marked seen by `source`; with `bindSpoolId` it binds it there. */
function readOn(uid: string | null, records: readonly S['NdefRecord'][], source: 'phone' | 'agent', bindSpoolId?: string | null): S['TagReadResult'] | Response {
  const decoded = decodeOpenSpool(records)
  if (bindSpoolId && uid === null) return problem(422, 'a tag with no UID cannot be bound', 'no_uid')
  if (uid === null) {
    const resolved = resolveRecords(records)
    return { tag: null, spool: null, decoded, draft: decoded ? draftOf(decoded) : null, resolved, next: resolved ? 'open_code' : 'none' }
  }
  const kind = decoded ? 'ntag_openspool' : null
  const tag = seenTag(uid, kind, source)
  if (bindSpoolId) {
    const bound = bindOn(bindSpoolId, uid, null, kind ?? tag.kind, false)
    if (bound instanceof Response) return bound
  }
  const spool = holderOf(tag.id)
  // A tag a spool holds opens the spool, whatever its records say: the binding is the stronger claim.
  const resolved = spool ? null : resolveRecords(records)
  return {
    tag: tagRowOf(tag),
    spool,
    decoded,
    draft: decoded && !spool ? draftOf(decoded) : null,
    resolved,
    next: spool ? 'open_spool' : resolved ? 'open_code' : 'bind_or_create',
  }
}

export const handlers = [
  http.get(`${BASE}/nfc-jobs`, () => {
    expireJobs()
    return HttpResponse.json(jobs.filter((one) => one.state === 'pending'))
  }),
  http.post(`${BASE}/nfc-jobs`, async ({ request }) => {
    const body = (await request.json()) as S['NfcJobIn']
    return HttpResponse.json(seedNfcJob({ kind: body.kind, records: body.records ?? undefined, ttl_s: body.ttl_s }), { status: 201 })
  }),
  http.get(`${BASE}/nfc-jobs/:id`, ({ params }) => {
    const job = jobOf(String(params.id))
    return job ? HttpResponse.json(job) : problem(404, `no NFC job ${String(params.id)}`)
  }),
  http.post(`${BASE}/nfc-jobs/:id/claim`, async ({ params, request }) => {
    const job = jobOf(String(params.id))
    if (!job) return problem(404, `no NFC job ${String(params.id)}`)
    const text = await request.text()
    const sha = text ? ((JSON.parse(text) as S['NfcJobClaimIn']).records_sha256 ?? null) : null
    if (job.state !== 'pending') return conflict('job_not_pending', 'the job is not pending')
    if (job.records === null) {
      if (sha !== null) return problem(422, 'a read job takes no records_sha256', 'records_sha256_not_taken')
    } else if (sha?.toLowerCase() !== (await recordsSha256(job.records))) return conflict('records_mismatch', 'the records are not the ones the page showed')
    Object.assign(job, { state: 'armed', armed_by: 'owner@example.com', expires_at: new Date(Date.now() + ARM_MS).toISOString() })
    return HttpResponse.json(job)
  }),
  http.post(`${BASE}/nfc-jobs/:id/cancel`, ({ params }) => {
    const job = jobOf(String(params.id))
    if (!job) return problem(404, `no NFC job ${String(params.id)}`)
    if (job.write_begun && job.state === 'armed') return conflict('write_begun', 'the write has begun: the job ends only with its result')
    if (job.state === 'pending' || job.state === 'armed') job.state = 'cancelled'
    return HttpResponse.json(job)
  }),
  http.post(`${BASE}/nfc-jobs/:id/result`, async ({ params, request }) => {
    const job = jobOf(String(params.id))
    if (!job) return problem(404, `no NFC job ${String(params.id)}`)
    if (job.state !== 'armed') return conflict('job_not_armed', 'the job is not armed')
    const body = (await request.json()) as S['NfcJobResultIn']
    if (body.error != null) return HttpResponse.json(Object.assign(job, { state: 'failed', result: { error: body.error } }))
    const uid = cleanTagUid(body.uid ?? null)
    const records = body.records ?? []
    if (job.kind === 'read') {
      const found = readOn(uid, records, 'agent')
      if (found instanceof Response) return found
      return HttpResponse.json(Object.assign(job, { state: 'done', result: { ...found, uid, records } }))
    }
    const backup = backups.find((one) => one.id === body.backup_id)
    if (!backup || uid === null || backup.reason !== 'agent_write' || backup.job_id !== job.id || backup.uid !== uid) return conflict('backup_mismatch', 'the backup does not cover this write')
    const result = { uid, records, backup_id: backup.id }
    if (!sameRecords(job.records ?? [], records)) return HttpResponse.json(Object.assign(job, { state: 'failed', result: { error: 'the tag does not hold what was sent', ...result } }))
    return HttpResponse.json(Object.assign(job, { state: 'done', result }))
  }),
  http.post(`${BASE}/tags/backups`, async ({ request }) => {
    const body = (await request.json()) as S['TagBackupIn']
    const uid = cleanTagUid(body.uid)
    if (uid === null) return problem(422, 'a tag with no UID cannot be backed up', 'no_uid')
    const tag = seenTag(uid, decodeOpenSpool(body.records) ? 'ntag_openspool' : null)
    const backup: Backup = { id: newId(), tag_id: tag.id, uid, records: body.records, reason: body.reason, job_id: body.job_id ?? null, actor: 'owner@example.com', at: new Date().toISOString() }
    backups.push(backup)
    const job = backup.job_id === null ? undefined : jobs.find((one) => one.id === backup.job_id)
    if (job && backup.reason === 'agent_write') job.write_begun = true
    return HttpResponse.json({ backup_id: backup.id } satisfies S['TagBackupMade'], { status: 201 })
  }),
  http.get(`${BASE}/tags/:id/backups`, ({ params }) => {
    if (!tagOf(String(params.id))) return problem(404, `no tag ${String(params.id)}`)
    return HttpResponse.json(backups.filter((one) => one.tag_id === params.id).reverse().slice(0, 50))
  }),
  // `records_for_spool` and `written` look the spool up first, then check the backup.
  http.post(`${BASE}/spools/:id/tag-records`, async ({ params, request }) => {
    const body = (await request.json()) as S['TagRecordsIn']
    const spool = spoolViewOf(String(params.id))
    if (!spool) return problem(404, `no spool ${String(params.id)}`)
    const backup = covering(body, 'spool_write')
    if (backup instanceof Response) return backup
    return HttpResponse.json(openSpoolRecords(spool))
  }),
  http.post(`${BASE}/spools/:id/tag-written`, async ({ params, request }) => {
    const body = (await request.json()) as S['TagWrittenIn']
    const spool = spoolViewOf(String(params.id))
    if (!spool) return problem(404, `no spool ${String(params.id)}`)
    const backup = covering(body, 'spool_write')
    if (backup instanceof Response) return backup
    if (!sameRecords(body.read_back, body.sent)) return conflict('read_back_mismatch', 'the tag does not hold what was sent')
    if (body.sent.length !== 1 || !decodeOpenSpool(body.sent)) return problem(422, 'the records sent are in no known format', 'unknown_format')
    if (!sameRecords(body.sent, openSpoolRecords(spool).records)) return conflict('sent_mismatch', 'sent is not what the spool gives now')
    // The read-back gives the tag its format's kind (R13).
    seenTag(backup.uid, 'ntag_openspool')
    const bound = bindOn(spool.id, backup.uid, null, 'ntag_openspool', false, false)
    if (bound instanceof Response) return bound
    // R11: the format's origin and the write's time.
    setOrigin(spool.id, 'openspool', new Date().toISOString())
    const tag = tagOf(bound.tag_id)!
    return HttpResponse.json({
      tag: tagRowOf(tag),
      spool: holderOf(tag.id),
      decoded: decodeOpenSpool(body.read_back),
      draft: null,
      next: 'open_spool',
    } satisfies S['TagReadResult'])
  }),
  http.post(`${BASE}/tags/:id/backups/:backup/restore`, async ({ params, request }) => {
    const tag = tagOf(String(params.id))
    const old = backups.find((one) => one.id === params.backup && one.tag_id === params.id)
    if (!tag || !old) return problem(404, `no backup ${String(params.backup)} of tag ${String(params.id)}`)
    const body = (await request.json()) as S['TagRecordsIn']
    const fresh = covering(body, 'restore')
    if (fresh instanceof Response) return fresh
    if (fresh.tag_id !== tag.id) return conflict('backup_mismatch', 'the fresh backup is of another tag')
    return HttpResponse.json({ format: decodeOpenSpool(old.records) ? 'openspool' : null, records: old.records, warnings: [] } satisfies S['TagRecords'])
  }),
  http.post(`${BASE}/tags/reads`, async ({ request }) => {
    const body = (await request.json()) as S['TagReadIn']
    const found = readOn(cleanTagUid(body.uid), body.records ?? [], 'phone', body.bind_spool_id)
    return found instanceof Response ? found : HttpResponse.json(found)
  }),
]
