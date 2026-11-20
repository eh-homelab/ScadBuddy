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
 * type but `tags` set to push (tags go through the refresh's drain). The seed holds one spool Bambuddy refused and one supplier held back
 * because Bambuddy has its name. Each "sync now" asks for a pass that, like the server's,
 * runs in the background: it lands on the second status read after the run (`READS_TO_LAND`),
 * a minute later than the last, with the refused spool failing once more. Ids and times are
 * fixed strings.
 */

const BASE = '/api/v1/filament/sync'
const FIRST_PASS = Date.parse('2026-11-15T07:00:00Z')
const MINUTE = 60_000

const seedErrors = (): S['SyncError'][] => [
  { kind: 'spool', sb_id: spoolId(3), label: '#3', error: 'https://scadbuddy.dev/problems/bambuddy-rejected:422', attempts: 2, at: '2026-11-15T07:00:00Z' },
  { kind: 'supplier', sb_id: SUPPLIER.MICRO, label: 'Micro Center', error: 'name_taken', attempts: 1, at: '2026-11-15T06:55:00Z' },
]

/** The status reads after a run before its pass lands (R-T11-2): not the first, so a page must read again. */
const READS_TO_LAND = 2

let passes = 0
let errors = seedErrors()
/** Status reads left before the pass asked for lands; null when none is running. */
let running: number | null = null

export function reset() {
  passes = 0
  errors = seedErrors()
  running = null
}

function land() {
  passes += 1
  // A held-back row is never sent, so only the refused one is tried again.
  errors = errors.map((error) => (error.error === 'name_taken' ? error : { ...error, attempts: error.attempts + 1, at: at(passes) }))
}

const at = (pass: number) => new Date(FIRST_PASS + pass * MINUTE).toISOString()

function skipped(): S['SyncStatus']['skipped'] {
  const settings = mockSettings()
  if (!settings.bambuddy_url) return 'no_bambuddy'
  // The server's `PUSHED_TYPES`: the sync pass never sends tags.
  const directions = Object.entries(settings.filament_sync ?? {}).filter(([kind]) => kind !== 'tags').map(([, direction]) => direction)
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
  http.get(`${BASE}/status`, () => {
    if (running !== null) {
      running -= 1
      if (running === 0) {
        running = null
        land()
      }
    }
    return HttpResponse.json(status())
  }),
  http.post(`${BASE}/run`, () => {
    restoreFilamentSync()
    const reason = skipped()
    if (reason === 'no_bambuddy') return conflict('no Bambuddy is configured', reason)
    if (reason === 'nothing_pushes') return conflict('no data type but tags is set to push to Bambuddy', reason)
    running ??= READS_TO_LAND
    return HttpResponse.json({ started: true } satisfies S['SyncRun'], { status: 202 })
  }),
]
