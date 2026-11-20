import { HttpResponse, http } from 'msw'
import type { components } from '../../../api/schema'
import { mockSettings, problem } from '../../handlers'
import { SUPPLIER } from '../filamentPurchasingSeed'
import { restoreFilamentSync } from '../settings'
import { spoolId } from './seed'

type S = components['schemas']

/**
 * #6265 — `GET /api/v1/filament/sync/status` and `POST /api/v1/filament/sync/run`
 * (`backend/scadbuddy/api/filament.py`). The directions are the stored `filament_sync`, and
 * `skipped` is worked out from the settings as the server does: no Bambuddy URL, or no data
 * type set to push. The seed holds one spool Bambuddy refused and one supplier held back
 * because Bambuddy has its name. Each "sync now" is a pass that finishes at once, a minute
 * later than the last, in which the refused spool fails once more. Ids and times are fixed strings.
 */

const BASE = '/api/v1/filament/sync'
const FIRST_PASS = Date.parse('2026-11-15T07:00:00Z')
const MINUTE = 60_000

const seedErrors = (): S['SyncError'][] => [
  { kind: 'spool', sb_id: spoolId(3), label: '#3', error: 'https://scadbuddy.dev/problems/bambuddy-rejected:422', attempts: 2, at: '2026-11-15T07:00:00Z' },
  { kind: 'supplier', sb_id: SUPPLIER.MICRO, label: 'Micro Center', error: 'name_taken', attempts: 1, at: '2026-11-15T06:55:00Z' },
]

let passes = 0
let errors = seedErrors()

export function reset() {
  passes = 0
  errors = seedErrors()
}

const at = (pass: number) => new Date(FIRST_PASS + pass * MINUTE).toISOString()

function skipped(): S['SyncStatus']['skipped'] {
  const settings = mockSettings()
  if (!settings.bambuddy_url) return 'no_bambuddy'
  const directions = Object.values(settings.filament_sync ?? {})
  return directions.some((direction) => direction === 'sb_to_bb' || direction === 'two_way') ? null : 'nothing_pushes'
}

function status(): S['SyncStatus'] {
  restoreFilamentSync()
  const settings = mockSettings()
  return {
    instance: settings.bambuddy_url ?? null,
    last_pass_at: at(passes),
    last_ok_at: at(passes),
    last_error: null,
    counts: { written: 4, failed: errors.length, deferred: 0, conflicts: 0, unmapped_k: 0 },
    errors,
    directions: settings.filament_sync as S['FilamentSync'],
    skipped: skipped(),
  }
}

const conflict = (detail: string, code: string) => problem(409, 'Conflict', detail, { code })

export const handlers = [
  http.get(`${BASE}/status`, () => HttpResponse.json(status())),
  http.post(`${BASE}/run`, () => {
    restoreFilamentSync()
    const reason = skipped()
    if (reason === 'no_bambuddy') return conflict('no Bambuddy is configured', reason)
    if (reason === 'nothing_pushes') return conflict('no data type is set to push to Bambuddy', reason)
    passes += 1
    // A held-back row is never sent, so only the refused one is tried again.
    errors = errors.map((error) => (error.error === 'name_taken' ? error : { ...error, attempts: error.attempts + 1, at: at(passes) }))
    return HttpResponse.json({ started: true } satisfies S['SyncRun'], { status: 202 })
  }),
]
