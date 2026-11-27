import { HttpResponse, http } from 'msw'
import type { CodeScan, ScanSource } from '../../api/scan'
import { newId } from '../newId'
import { resolveCodeText, resolvedSpool, spoolRowOf } from './filament'

/**
 * #8155 — core's code scans (`tools/go/core/internal/scan`). The mock keeps each photo's bytes
 * and its type, but reads no code from them: a scan answers the codes a test queued
 * (`queueScanCodes`), else none. Each code resolves as core resolves it: a spool label's
 * `scadbuddy:spool:<id>`, a place label's `<origin>/x/<code>`, or a typed code.
 */
const PROBLEMS = 'about:blank'

interface Photo {
  bytes: ArrayBuffer
  type: string
}

const state: { photos: Map<string, Photo>; scans: CodeScan[]; queued: { format: string; text: string }[][] } = {
  photos: new Map(),
  scans: [],
  queued: [],
}

export function reset(): void {
  state.photos = new Map()
  state.scans = []
  state.queued = []
}

/** The codes the next scan reads, one list per scan, in order. */
export function queueScanCodes(...scans: { format: string; text: string }[][]): void {
  state.queued.push(...scans)
}

const SPOOL_TEXT = /^scadbuddy:spool:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i
const PLACE_URL = /^https?:\/\/[^/?#\s]+\/x\/([A-Za-z]-?[0-9]{1,10})\/?$/

function resolve(text: string) {
  const spool = SPOOL_TEXT.exec(text)
  if (spool) {
    const row = spoolRowOf(spool[1]!.toLowerCase())
    return row ? resolvedSpool(row) : null
  }
  return resolveCodeText(PLACE_URL.exec(text)?.[1] ?? text)
}

const withResolved = (scan: CodeScan): CodeScan => ({ ...scan, codes: scan.codes.map((code) => ({ ...code, resolved: resolve(code.text) })) })

const problem = (status: number, reason: string, detail: string) =>
  HttpResponse.json({ type: PROBLEMS, title: 'Error', status, detail, reason }, { status, headers: { 'Content-Type': 'application/problem+json' } })

async function sha256(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('')
}

export const handlers = [
  http.post('/api/v1/scan/images', async ({ request }) => {
    const bytes = await request.arrayBuffer()
    const type = request.headers.get('Content-Type') ?? ''
    if (bytes.byteLength === 0 || !/^image\/(jpeg|png|webp)$/.test(type)) {
      return problem(415, 'image_not_supported', 'the body is not a JPEG, PNG or WebP image')
    }
    const sum = await sha256(bytes)
    const key = `scan_image-${sum}`
    state.photos.set(key, { bytes, type })
    return HttpResponse.json({ key, media_type: type, sha256: sum, size: bytes.byteLength })
  }),

  http.post('/api/v1/scan/codes', async ({ request }) => {
    const body = (await request.json()) as { key: string; source?: ScanSource; location?: CodeScan['location'] }
    const photo = state.photos.get(body.key)
    if (!photo) return problem(404, 'image_not_found', 'no such scan photo')
    const at = new Date().toISOString()
    const scan: CodeScan = {
      id: newId(),
      at,
      source: body.source ?? 'shutter',
      actor: 'user:mock#person',
      image: { key: body.key, media_type: photo.type },
      width: null,
      height: null,
      codes: (state.queued.shift() ?? []).map((code) => ({ ...code, resolved: null })),
      error: null,
      location: body.location ?? null,
      decoded_at: at,
    }
    state.scans.unshift(scan)
    return HttpResponse.json(withResolved(scan))
  }),

  http.get('/api/v1/scan/scans', () => HttpResponse.json({ scans: state.scans.map(withResolved) })),

  http.get('/api/v1/scan/scans/:id', ({ params }) => {
    const scan = state.scans.find((one) => one.id === params.id)
    return scan ? HttpResponse.json(withResolved(scan)) : problem(404, 'scan_not_found', 'no such scan')
  }),

  http.get('/api/v1/scan/scans/:id/image', ({ params }) => {
    const scan = state.scans.find((one) => one.id === params.id)
    const photo = scan && state.photos.get(scan.image.key)
    return photo ? new HttpResponse(photo.bytes, { headers: { 'Content-Type': photo.type } }) : problem(404, 'scan_not_found', 'no such scan')
  }),

  http.post('/api/v1/scan/scans/:id/decode', ({ params }) => {
    const scan = state.scans.find((one) => one.id === params.id)
    if (!scan) return problem(404, 'scan_not_found', 'no such scan')
    const queued = state.queued.shift()
    if (queued) scan.codes = queued.map((code) => ({ ...code, resolved: null }))
    scan.decoded_at = new Date().toISOString()
    return HttpResponse.json(withResolved(scan))
  }),

  http.delete('/api/v1/scan/scans/:id', ({ params }) => {
    const before = state.scans.length
    state.scans = state.scans.filter((one) => one.id !== params.id)
    return state.scans.length === before ? problem(404, 'scan_not_found', 'no such scan') : new HttpResponse(null, { status: 204 })
  }),
]
