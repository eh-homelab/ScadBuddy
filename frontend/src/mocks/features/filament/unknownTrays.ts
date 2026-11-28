import { HttpResponse, http } from 'msw'
import type { UnknownSpoolCandidate, UnknownSpoolTray, UnknownSpoolTrays } from '../../../api/unknownTrays'
import { inventoryRows, placeSpoolAtTray } from './index'
import { H2C } from './seed'

/**
 * #7143 — the unknown-spool routes (`tools/go/core/internal/unknowntrays`) over the filament
 * mock's spools: an unmatched tray on the H2C (AMS 0, slot 2, PLA red, no remain; `seedUnknownTray`), the ranked
 * candidates (unplaced first, then by number, at most 10), resolve and dismiss. A resolve
 * links the spool through `placeSpoolAtTray` and drops the tray. `seen_at` carries microseconds,
 * as the server's does.
 */

const BASE = '/api/v1/filament/unknown-trays'
const MAX_CANDIDATES = 10
const SEEN_AT = '2026-11-26T09:30:00.123456Z'

const problem = (status: number, code: string, detail: string) =>
  HttpResponse.json({ type: 'about:blank', title: detail, status, detail, code }, { status, headers: { 'Content-Type': 'application/problem+json' } })

function seededTray(): UnknownSpoolTray {
  return {
    id: '00000000-0000-4000-8000-00000000a143',
    printer_id: H2C.printer_id,
    printer_name: H2C.printer_name,
    ams_id: 0,
    tray_id: 1,
    slot: 'AMS-A slot 2',
    place_id: null,
    place_name: null,
    place_code: null,
    material: 'PLA',
    colour_rgba: '#FF0000FF',
    remain_pct: null,
    first_seen_at: '2026-11-26T09:00:00.000000Z',
    seen_at: SEEN_AT,
    candidates: [],
  }
}

/**
 * Empty until a test or a scenario asks for the tray (`seedUnknownTray`): a tray that waits adds
 * one to every "Needs you" count and to the mocked app's tab title, which other tests pin.
 */
let trays: UnknownSpoolTray[] = []

/**
 * A Playwright test puts the tray before the page loads: `"1"` under this `sessionStorage` key,
 * read once at load (as `pendingInput.ts` does).
 */
export const UNKNOWN_TRAY_SEED_KEY = 'scadbuddy.mock.unknown-tray'
try {
  if (typeof sessionStorage !== 'undefined' && sessionStorage.getItem(UNKNOWN_TRAY_SEED_KEY) === '1') trays = [seededTray()]
} catch {
  // Nothing seeded.
}

/** The trays the print block names, as the print check's `unknown_trays` gives them. */
export const unknownTrayRefs = (): { id: string; slot: string }[] => trays.map(({ id, slot }) => ({ id, slot }))

export function reset() {
  trays = []
  resolvesOff = false
  pendingNext = false
}

/** Puts the one unmatched tray (H2C, AMS-A slot 2) in the mock; returns it with its candidates. */
export function seedUnknownTray(): UnknownSpoolTray {
  trays = [seededTray()]
  return { ...trays[0]!, candidates: candidates() }
}

function candidates(): UnknownSpoolCandidate[] {
  return inventoryRows()
    .map(({ spool }): UnknownSpoolCandidate => ({
      spool_id: spool.id,
      number: spool.number,
      code: `S-${spool.number}`,
      material: spool.names?.material ?? null,
      colour_name: spool.effective?.colour_name ?? null,
      rgba: spool.effective?.rgba ?? null,
      label_weight_g: spool.effective?.label_weight_g ?? null,
      grams_left: spool.grams_left ?? null,
      place_id: spool.location_id ?? null,
      place_name: spool.names?.location ?? null,
      colour_distance: null,
    }))
    .sort((a, b) => Number(a.place_id !== null) - Number(b.place_id !== null) || a.number - b.number)
    .slice(0, MAX_CANDIDATES)
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const MAX_NET_WEIGHT_G = 100000
const BODY_KEYS = new Set(['spool_id', 'code', 'net_weight_g', 'seen_at'])
/** RFC 3339 with a zone, as Go's `time.RFC3339Nano` parse takes it. */
const RFC3339 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/

/** Go's check of the body (`resolve.go` `resolveFields`): why it is refused, or null. */
function badBody(body: unknown): string | null {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) return 'the body is not a JSON object'
  const fields = body as Record<string, unknown>
  const unknown = Object.keys(fields).find((key) => !BODY_KEYS.has(key))
  if (unknown !== undefined) return `the field "${unknown}" is not known`
  const spool = fields.spool_id ?? null
  const code = fields.code ?? null
  if (spool !== null && (typeof spool !== 'string' || !UUID.test(spool))) return 'spool_id: not a UUID'
  if (code !== null && (typeof code !== 'string' || code.trim() === '')) return 'code: not a spool code'
  if ((spool === null) === (code === null)) return 'give exactly one of spool_id and code'
  const grams = fields.net_weight_g
  if (typeof grams !== 'number') return 'net_weight_g: the field is required, as a number'
  if (!(grams > 0 && grams <= MAX_NET_WEIGHT_G)) return `net_weight_g: more than 0 and at most ${MAX_NET_WEIGHT_G}`
  if (typeof fields.seen_at !== 'string') return 'seen_at: the field is required, as an RFC 3339 time'
  if (!RFC3339.test(fields.seen_at)) return 'seen_at: not an RFC 3339 time with a zone'
  return null
}

/** Core with no Temporal address: every resolve answers 503 `writes_not_configured`. */
let resolvesOff = false
export const resolveWritesOff = (off = true) => {
  resolvesOff = off
}

/** The next resolve answers 503 `resolve_pending`, as one that did not answer in time. */
let pendingNext = false
export const resolvePendingOnce = () => {
  pendingNext = true
}

const codeNumber = (text: string): number | null => {
  const hit = /^S-?(\d+)$/.exec(text.trim().toUpperCase())
  return hit ? Number(hit[1]) : null
}

export const handlers = [
  http.get(BASE, () => HttpResponse.json({ trays: trays.map((tray) => ({ ...tray, candidates: candidates() })), made_from_tags: [] } satisfies UnknownSpoolTrays)),
  http.post(`${BASE}/:id/resolve`, async ({ params, request }) => {
    // Go's order (`resolve.go`): the path's id, then Temporal, then the body, then the activity.
    if (!UUID.test(String(params.id))) return problem(409, 'tray_changed', 'The tray changed after the list was read, or it has a spool now. Read the list again.')
    if (resolvesOff) return problem(503, 'writes_not_configured', 'Resolving an unknown tray needs Temporal, and this server has no address for it.')
    const sent: unknown = await request.json().catch(() => undefined)
    const bad = badBody(sent)
    if (bad) return problem(422, 'invalid_input', bad)
    if (pendingNext) {
      pendingNext = false
      return problem(503, 'resolve_pending', 'The link did not answer in time, and it may still run. Read the list of unknown trays, then send it again if the tray is still there.')
    }
    const body = sent as { spool_id?: string | null; code?: string | null; net_weight_g: number; seen_at: string }
    const tray = trays.find((one) => one.id === params.id)
    if (!tray || tray.seen_at !== body.seen_at) return problem(409, 'tray_changed', 'the tray changed or was resolved')
    const spools = inventoryRows(true).map((row) => row.spool)
    const byId = body.spool_id != null
    const spool = byId ? spools.find((one) => one.id === body.spool_id) : spools.find((one) => one.number === codeNumber(body.code ?? ''))
    if (!spool) return byId ? problem(404, 'spool_missing', 'That spool does not exist.') : problem(404, 'code_unknown', 'No spool has that code.')
    if (spool.archived_at) return problem(409, 'spool_archived', 'That spool is archived.')
    const resolved = placeSpoolAtTray(spool.id, tray.printer_id, tray.ams_id, tray.tray_id)
    if (!resolved) return problem(404, 'spool_missing', 'no such spool')
    trays = trays.filter((one) => one.id !== tray.id)
    return HttpResponse.json(resolved)
  }),
  http.post(`${BASE}/:id/dismiss`, ({ params }) => {
    if (!trays.some((one) => one.id === params.id)) return problem(404, 'tray_gone', 'the tray is gone')
    trays = trays.filter((one) => one.id !== params.id)
    return new HttpResponse(null, { status: 204 })
  }),
]
