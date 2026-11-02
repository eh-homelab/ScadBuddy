import { http, HttpResponse } from 'msw'
import type { NativeSpool, NativeSpoolPatch } from '../../api/types'

/**
 * ScadBuddy's own spools (#4643; backend `api/native_spools.py`, `spools/native.py`):
 * oldest first, archived ones only with `include_archived`, one unarchived spool per tag
 * (409), and the tags cleaned as the backend cleans them (hex upper case, all zeros none).
 */

const base = '/api/v1/native-spools'

/** The fixture's spool: Bambu PLA in red, 812 g of 1000 g left, tagged. */
export const NATIVE_SPOOL_RED = '0d3c7a52-4643-4000-8000-000000000001'
const STARTED = '2026-11-01T12:00:00Z'

function fixture(): NativeSpool[] {
  return [
    {
      id: NATIVE_SPOOL_RED,
      material: 'PLA',
      colour: '#FF0000',
      brand: 'Bambu',
      name: 'Red',
      label_weight_g: 1000,
      remaining_g: 812,
      tray_uuid: 'A1B2C3D4E5F60718293A4B5C6D7E8F90',
      tag_uid: null,
      preset: 'GFA00',
      note: null,
      archived_at: null,
      created_at: STARTED,
      updated_at: STARTED,
    },
  ]
}

let spools = fixture()
let next = 2

export function reset(): void {
  spools = fixture()
  next = 2
}

const FIELDS = ['material', 'colour', 'brand', 'name', 'label_weight_g', 'remaining_g', 'tray_uuid', 'tag_uid', 'preset', 'note'] as const

function tag(value: unknown, length: number, last: boolean): string | null {
  if (typeof value !== 'string') return null
  const hex = value.replace(/[^0-9a-fA-F]/g, '').toUpperCase()
  const cut = last ? hex.slice(-length) : hex.slice(0, length)
  return cut === '' || /^0+$/.test(cut) ? null : cut
}

function cleaned(body: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const name of FIELDS) {
    if (!(name in body)) continue
    const value = body[name]
    if (name === 'tray_uuid') out[name] = tag(value, 32, false)
    else if (name === 'tag_uid') out[name] = tag(value, 16, true)
    else if (name === 'colour') out[name] = typeof value === 'string' ? value.toUpperCase() : null
    else out[name] = value ?? null
  }
  return out
}

function refused(request: Request, status: number, detail: string) {
  return HttpResponse.json(
    { type: 'about:blank', title: status === 404 ? 'Not Found' : 'Conflict', status, detail, instance: new URL(request.url).pathname },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )
}

function taken(spool: Pick<NativeSpool, 'tray_uuid' | 'tag_uid'>, self?: string): boolean {
  return spools.some(
    (other) =>
      other.id !== self &&
      !other.archived_at &&
      ((spool.tray_uuid && other.tray_uuid === spool.tray_uuid) || (spool.tag_uid && other.tag_uid === spool.tag_uid)),
  )
}

const TAKEN = 'another spool on the shelf carries that tag'

export const handlers = [
  http.get(base, ({ request }) => {
    const archived = new URL(request.url).searchParams.get('include_archived') === 'true'
    return HttpResponse.json(spools.filter((s) => archived || !s.archived_at))
  }),
  http.post(base, async ({ request }) => {
    const body = cleaned((await request.json()) as Record<string, unknown>)
    const at = new Date().toISOString()
    const spool = {
      id: `0d3c7a52-4643-4000-8000-${String(next++).padStart(12, '0')}`,
      ...Object.fromEntries(FIELDS.map((name) => [name, null])),
      ...body,
      archived_at: null,
      created_at: at,
      updated_at: at,
    } as NativeSpool
    if (taken(spool)) return refused(request, 409, TAKEN)
    spools.push(spool)
    return HttpResponse.json(spool, { status: 201 })
  }),
  http.get(`${base}/:id`, ({ request, params }) => {
    const spool = spools.find((s) => s.id === params.id)
    return spool ? HttpResponse.json(spool) : refused(request, 404, `no spool ${String(params.id)} on ScadBuddy's shelf`)
  }),
  http.patch(`${base}/:id`, async ({ request, params }) => {
    const index = spools.findIndex((s) => s.id === params.id)
    if (index < 0) return refused(request, 404, `no spool ${String(params.id)} on ScadBuddy's shelf`)
    const body = (await request.json()) as NativeSpoolPatch
    const was = spools[index]!
    const archived_at = body.archived === true ? (was.archived_at ?? new Date().toISOString()) : body.archived === false ? null : was.archived_at
    const spool: NativeSpool = { ...was, ...cleaned(body as Record<string, unknown>), archived_at, updated_at: new Date().toISOString() }
    if (!spool.archived_at && taken(spool, spool.id)) return refused(request, 409, TAKEN)
    spools[index] = spool
    return HttpResponse.json(spool)
  }),
  http.delete(`${base}/:id`, ({ request, params }) => {
    const index = spools.findIndex((s) => s.id === params.id)
    if (index < 0) return refused(request, 404, `no spool ${String(params.id)} on ScadBuddy's shelf`)
    spools.splice(index, 1)
    return new HttpResponse(null, { status: 204 })
  }),
]
