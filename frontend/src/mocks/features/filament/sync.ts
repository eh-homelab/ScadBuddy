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
 * at that read's time, with the refused spool failing once more. Ids and the seed's times
 * are fixed strings.
 *
 * #6773 — `GET`/`POST /api/v1/filament/sync/reset-links`. By default the links belong to the
 * configured Bambuddy; `linkOtherBambuddy` makes them another's (`instance_changed`), as after a
 * change of the Bambuddy URL. The reset checks `confirm_instance` as the server does, deletes the
 * links, the intents and the error rows, and asks for a pass that lands as "Sync now"'s does.
 * #6845 — with `{ same: true }` the configured Bambuddy is the links' own at a new address
 * (`same_bambuddy`): a reset with `expect_kept` deletes nothing and the links belong to the
 * configured one; `loseSameBambuddy` makes it another before the reset (409 `proof_changed`), and
 * `{ unreadable: true }` a Bambuddy the dry run cannot read (`bambuddy_unreadable`). A reset
 * without `expect_kept` deletes, as the server's does.
 */

const BASE = '/api/v1/filament/sync'
const FIRST_PASS = '2026-11-15T07:00:00Z'

const seedErrors = (): S['SyncError'][] => [
  { kind: 'spool', sb_id: spoolId(3), label: '#3', error: 'https://scadbuddy.dev/problems/bambuddy-rejected:422', attempts: 2, at: '2026-11-15T07:00:00Z' },
  { kind: 'supplier', sb_id: SUPPLIER.MICRO, label: 'Micro Center', error: 'name_taken', attempts: 1, at: '2026-11-15T06:55:00Z' },
]

/** The status reads after a run before its pass lands (R-T11-2): not the first, so a page must read again. */
const READS_TO_LAND = 2

let lastPassAt = FIRST_PASS
let lastPassStartedAt = FIRST_PASS
/** The server's time of the run that is waiting for its pass (`requested_at`). */
let requestedAt: string | null = null
let errors = seedErrors()
/** Status reads left before the pass asked for lands; null when none is running. */
let running: number | null = null
/** The other Bambuddy the links belong to (#6773); null while they belong to the configured one. */
let otherInstance: string | null = null
/** No links at all, after a reset and before the pass that links the new Bambuddy. */
let unlinked = false
/** #6845 — the configured Bambuddy holds the linked spools: a reset keeps the links. */
let sameBambuddy = false
/** #6845 — the configured Bambuddy cannot be read. */
let unreadable = false

/**
 * #6905 — `GET /api/v1/filament/sync/passes`: the pass history, newest first. The seed holds
 * 14 passes ending at the seed's last pass: clean ones on the timer, one that failed on
 * ScadBuddy's own API (`crud_unavailable:transport`) and one skipped, so the list has a second
 * page at 10. A pass that lands is added on top. The cursor is the index of the next row.
 */
const passId = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`

function seedPasses(): S['SyncPass'][] {
  const end = new Date(FIRST_PASS).getTime()
  return Array.from({ length: 14 }, (_, i) => {
    const ended = new Date(end - i * 5 * 60_000)
    const started = new Date(ended.getTime() - 4_000)
    const base: S['SyncPass'] = {
      id: passId(i + 1),
      instance: '0123456789ab',
      trigger: i === 0 ? 'full' : 'timer',
      started_at: started.toISOString(),
      ended_at: ended.toISOString(),
      written: i === 0 ? 4 : 0,
      failed: i === 0 ? 2 : 0,
      deferred: 0,
      conflicts: 0,
      unmapped_k: 0,
      skipped: null,
      instance_changed: false,
      error: null,
    }
    if (i === 2) return { ...base, trigger: 'change', error: 'crud_unavailable:transport' }
    if (i === 3) return { ...base, skipped: 'nothing_pushes' }
    return base
  })
}

let passes = seedPasses()

/** What the seed's links number when they belong to another Bambuddy. */
const SEED_LINKS = { spools: 12, suppliers: 2, locations: 3, insert_types: 1, filaments: 4, intents: 1, ignored: 0 }

export function reset() {
  lastPassAt = FIRST_PASS
  lastPassStartedAt = FIRST_PASS
  requestedAt = null
  errors = seedErrors()
  running = null
  otherInstance = null
  unlinked = false
  sameBambuddy = false
  unreadable = false
  passes = seedPasses()
}

/**
 * #6773 — the links now belong to `instance`, not the configured Bambuddy, so the sync is stopped.
 * #6845 — `same`: the configured Bambuddy is that one at a new address, so a reset keeps the links;
 * `unreadable`: the configured Bambuddy cannot be read.
 */
export function linkOtherBambuddy(
  instance = '0ld0ld0ld0ld',
  { same = false, unreadable: down = false }: { same?: boolean; unreadable?: boolean } = {},
) {
  otherInstance = instance
  unlinked = false
  sameBambuddy = same && !down
  unreadable = down
}

/** #6845 — the configured Bambuddy changed after the dry run: it no longer proves the links' own. */
export function loseSameBambuddy() {
  sameBambuddy = false
}

/** The configured Bambuddy's instance, as `instance_for` would name it (the mock uses the URL). */
const configured = () => mockSettings().bambuddy_url ?? null

function linksInstance(): string | null {
  if (unlinked) return null
  return otherInstance ?? configured()
}

function resetCounts(): S['LinksResetCounts'] {
  if (unlinked) return { spools: 0, suppliers: 0, locations: 0, insert_types: 0, filaments: 0, intents: 0, errors: 0, ignored: 0 }
  return { ...SEED_LINKS, errors: errors.length }
}

function land() {
  // Now, as the server's pass ends after the run that asked for it (the page waits for a later one).
  lastPassAt = new Date().toISOString()
  // A pass after a reset links the configured Bambuddy.
  unlinked = false
  // The pass the run asked for starts at or after the request.
  lastPassStartedAt = requestedAt ?? lastPassAt
  // A held-back row is never sent, so only the refused one is tried again.
  errors = errors.map((error) => (error.error === 'name_taken' ? error : { ...error, attempts: error.attempts + 1, at: lastPassAt }))
  // #6905 — and the pass is a row of the history, on top.
  passes = [
    {
      id: passId(1000 + passes.length),
      instance: configured(),
      trigger: 'full',
      started_at: lastPassStartedAt,
      ended_at: lastPassAt,
      written: 4,
      failed: errors.filter((error) => error.error !== 'name_taken').length,
      deferred: 0,
      conflicts: 0,
      unmapped_k: 0,
      skipped: null,
      instance_changed: false,
      error: null,
    },
    ...passes,
  ]
}

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
  const instance = linksInstance()
  return {
    instance,
    instance_changed: instance !== null && configured() !== null && instance !== configured(),
    last_pass_at: lastPassAt,
    last_pass_started_at: lastPassStartedAt,
    last_ok_at: lastPassAt,
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
  http.get(`${BASE}/passes`, ({ request }) => {
    const params = new URL(request.url).searchParams
    const limit = Number(params.get('limit') ?? 20)
    const cursor = params.get('cursor')
    const from = cursor === null ? 0 : Number(cursor)
    if (!Number.isInteger(from) || from < 0) return problem(422, 'Unprocessable Content', 'the cursor is not one this pass history gave')
    if (!Number.isInteger(limit) || limit < 1 || limit > 200) return problem(422, 'Unprocessable Content', 'limit must be 1 to 200')
    const page = passes.slice(from, from + limit)
    const next = from + limit < passes.length ? String(from + limit) : null
    return HttpResponse.json({ passes: page, next_cursor: next } satisfies S['SyncPassPage'])
  }),
  http.post(`${BASE}/run`, () => {
    restoreFilamentSync()
    const reason = skipped()
    if (reason === 'no_bambuddy') return conflict('no Bambuddy is configured', reason)
    if (reason === 'nothing_pushes') return conflict('no data type the sync pushes is set to push to Bambuddy (filament_sync; tags go with the refresh)', reason)
    running ??= READS_TO_LAND
    requestedAt = new Date().toISOString()
    return HttpResponse.json({ started: true, requested_at: requestedAt } satisfies S['SyncRun'], { status: 202 })
  }),
  http.get(`${BASE}/reset-links`, () =>
    HttpResponse.json({
      instance: linksInstance(),
      configured_instance: configured(),
      counts: resetCounts(),
      same_bambuddy: sameBambuddy && otherInstance !== null,
      bambuddy_unreadable: unreadable,
    } satisfies S['LinksResetPreview']),
  ),
  http.post(`${BASE}/reset-links`, async ({ request }) => {
    const body = (await request.json()) as S['LinksResetRequest']
    const instance = linksInstance()
    if (instance === null) return conflict('no Bambuddy instance is recorded, so no link belongs to one', 'nothing_to_reset')
    if (instance === configured()) return conflict('the links belong to the configured Bambuddy; nothing to reset', 'same_instance')
    if (body.confirm_instance !== instance) return conflict('confirm_instance is not the instance the links belong to', 'confirm_mismatch')
    if (body.expect_kept) {
      if (!sameBambuddy) {
        return conflict(
          'the configured Bambuddy could not be read, or is no longer proven the one the links were made against; nothing was changed, so read the dry run again',
          'proof_changed',
        )
      }
      otherInstance = null
      sameBambuddy = false
      running ??= READS_TO_LAND
      const none = { spools: 0, suppliers: 0, locations: 0, insert_types: 0, filaments: 0, intents: 0, errors: 0, ignored: 0 }
      return HttpResponse.json({ instance, kept: true, deleted: none } satisfies S['LinksResetDone'])
    }
    const deleted = resetCounts()
    otherInstance = null
    unlinked = true
    errors = []
    running ??= READS_TO_LAND
    return HttpResponse.json({ instance, kept: false, deleted } satisfies S['LinksResetDone'])
  }),
]
