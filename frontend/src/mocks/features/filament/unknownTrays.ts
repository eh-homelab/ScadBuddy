import { HttpResponse, http } from 'msw'
import type { UnknownSpoolCandidate, UnknownSpoolResolve, UnknownSpoolTray, UnknownSpoolTrays } from '../../../api/unknownTrays'
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

export function reset() {
  trays = []
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

const codeNumber = (text: string): number | null => {
  const hit = /^S-?(\d+)$/.exec(text.trim().toUpperCase())
  return hit ? Number(hit[1]) : null
}

export const handlers = [
  http.get(BASE, () => HttpResponse.json({ trays: trays.map((tray) => ({ ...tray, candidates: candidates() })), made_from_tags: [] } satisfies UnknownSpoolTrays)),
  http.post(`${BASE}/:id/resolve`, async ({ params, request }) => {
    const body = (await request.json()) as UnknownSpoolResolve
    const tray = trays.find((one) => one.id === params.id)
    if (!tray || tray.seen_at !== body.seen_at) return problem(409, 'tray_changed', 'the tray changed since the list was read')
    const spools = inventoryRows().map((row) => row.spool)
    const spool = 'spool_id' in body ? spools.find((one) => one.id === body.spool_id) : spools.find((one) => one.number === codeNumber(body.code))
    if (!spool) return 'spool_id' in body ? problem(404, 'spool_missing', 'no such spool') : problem(404, 'code_unknown', `no spool has the code ${body.code}`)
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
