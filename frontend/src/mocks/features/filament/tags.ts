import { HttpResponse, http } from 'msw'
import type { components } from '../../../api/schema'
import { parseRgba } from '../../../lib/rgba'
import { bindOn, cleanTagUid, findByKey, holderOf, seenTag, tagRowOf } from './index'

type S = components['schemas']

/**
 * #7273 — `POST /api/v1/filament/tags/reads` (`backend/scadbuddy/filament/tags.py` `read_on`)
 * over the seeded tags, which `index.ts` owns. A read with a UID finds or makes its tag; with
 * `bind_spool_id` it binds it there, through the tag route's own `bindOn`. `next` is
 * `open_spool` when a spool holds the tag, `bind_or_create` for a tag no spool holds, and
 * `none` with no UID. Only OpenSpool's JSON is decoded here; an OpenTag3D record reads as no format.
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

export const handlers = [
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
