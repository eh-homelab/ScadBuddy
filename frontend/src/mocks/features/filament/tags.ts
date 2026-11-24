import { HttpResponse, http } from 'msw'
import type { components } from '../../../api/schema'
import { parseRgba } from '../../../lib/rgba'
import { newId } from '../../newId'
import { bindOn, cleanTagUid, findByKey, holderOf, seenTag, spoolViewOf, tagOf, tagRowOf } from './index'

type S = components['schemas']

/**
 * #7273 — `POST /api/v1/filament/tags/reads` (`backend/scadbuddy/filament/tags.py` `read_on`)
 * over the seeded tags, which `index.ts` owns. A read with a UID finds or makes its tag; with
 * `bind_spool_id` it binds it there, through the tag route's own `bindOn`. `next` is
 * `open_spool` when a spool holds the tag, `bind_or_create` for a tag no spool holds, and
 * `none` with no UID. Only OpenSpool's JSON is decoded here; an OpenTag3D record reads as no format.
 *
 * The write routes (`back_up`, `records_for_spool`, `written`, `restore_records`) keep each
 * tag's backups in memory, append-only. `tag-records` answers one OpenSpool record built from
 * the spool; `tag-written` checks the backup, the read-back and the records, then binds.
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

export function reset() {
  backups = []
}

const hexOfText = (value: string) => Array.from(new TextEncoder().encode(value), (byte) => byte.toString(16).padStart(2, '0')).join('')

/** `sameRecords`: equal `(record_type, media_type, data_hex)` in order. */
const sameRecords = (a: readonly S['NdefRecord'][], b: readonly S['NdefRecord'][]) =>
  a.length === b.length &&
  a.every((one, at) => one.record_type === b[at]!.record_type && (one.media_type ?? null) === (b[at]!.media_type ?? null) && one.data_hex.toLowerCase() === b[at]!.data_hex.toLowerCase())

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

/** `_covering_backup`: the backup of this write, of the tag as just read, with this reason. */
function covering(body: { uid: string; backup_id: string; records?: S['NdefRecord'][] }, reason: Backup['reason']): Backup | Response {
  const backup = backups.find((one) => one.id === body.backup_id)
  if (!backup || backup.reason !== reason || cleanTagUid(backup.uid) !== cleanTagUid(body.uid) || (body.records && !sameRecords(backup.records, body.records))) {
    return conflict('backup_mismatch', 'the backup does not cover this write')
  }
  return backup
}

export const handlers = [
  http.post(`${BASE}/tags/backups`, async ({ request }) => {
    const body = (await request.json()) as S['TagBackupIn']
    const uid = cleanTagUid(body.uid)
    if (uid === null) return problem(422, 'a tag with no UID cannot be backed up', 'no_uid')
    const tag = seenTag(uid, decodeOpenSpool(body.records) ? 'ntag_openspool' : null)
    const backup: Backup = { id: newId(), tag_id: tag.id, uid, records: body.records, reason: body.reason, job_id: body.job_id ?? null, actor: 'owner@example.com', at: new Date().toISOString() }
    backups.push(backup)
    return HttpResponse.json({ backup_id: backup.id } satisfies S['TagBackupMade'], { status: 201 })
  }),
  http.get(`${BASE}/tags/:id/backups`, ({ params }) => {
    if (!tagOf(String(params.id))) return problem(404, `no tag ${String(params.id)}`)
    return HttpResponse.json(backups.filter((one) => one.tag_id === params.id).reverse().slice(0, 50))
  }),
  http.post(`${BASE}/spools/:id/tag-records`, async ({ params, request }) => {
    const spool = spoolViewOf(String(params.id))
    if (!spool) return problem(404, `no spool ${String(params.id)}`)
    const body = (await request.json()) as S['TagRecordsIn']
    const backup = covering(body, 'spool_write')
    if (backup instanceof Response) return backup
    return HttpResponse.json(openSpoolRecords(spool))
  }),
  http.post(`${BASE}/spools/:id/tag-written`, async ({ params, request }) => {
    const spool = spoolViewOf(String(params.id))
    if (!spool) return problem(404, `no spool ${String(params.id)}`)
    const body = (await request.json()) as S['TagWrittenIn']
    const backup = covering(body, 'spool_write')
    if (backup instanceof Response) return backup
    if (!sameRecords(body.read_back, body.sent)) return conflict('read_back_mismatch', 'the tag does not hold what was sent')
    if (!sameRecords(body.sent, openSpoolRecords(spool).records)) return conflict('sent_mismatch', 'sent is not what the spool gives now')
    // The read-back gives the tag its format's kind (R13).
    seenTag(backup.uid, 'ntag_openspool')
    const bound = bindOn(spool.id, backup.uid, null, 'ntag_openspool', false)
    if (bound instanceof Response) return bound
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
    const uid = cleanTagUid(body.uid)
    const decoded = decodeOpenSpool(body.records ?? [])
    if (body.bind_spool_id && uid === null) return problem(422, 'a tag with no UID cannot be bound', 'no_uid')
    if (uid === null) {
      return HttpResponse.json({ tag: null, spool: null, decoded, draft: decoded ? draftOf(decoded) : null, next: 'none' } satisfies S['TagReadResult'])
    }
    const kind = decoded ? 'ntag_openspool' : null
    const tag = seenTag(uid, kind)
    if (body.bind_spool_id) {
      const bound = bindOn(body.bind_spool_id, uid, null, kind ?? tag.kind, false)
      if (bound instanceof Response) return bound
    }
    const spool = holderOf(tag.id)
    return HttpResponse.json({
      tag: tagRowOf(tag),
      spool,
      decoded,
      draft: decoded && !spool ? draftOf(decoded) : null,
      next: spool ? 'open_spool' : 'bind_or_create',
    } satisfies S['TagReadResult'])
  }),
]
