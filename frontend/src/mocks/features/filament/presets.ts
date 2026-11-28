import { HttpResponse, http } from 'msw'
import type { PresetDetail, PresetDiff, PresetEdit, PresetKeyChange, PresetRow, PresetSource, PresetVersion } from '../../../api/filamentPresets'
import { T } from './seed'

/**
 * #7204 — the preset routes (`tools/go/core/internal/filamentpresets`): the list, a preset and the
 * diff of two of its versions. Seeded with a `3dfp` preset (sent), a `bambuddy` one, a `manual`
 * one with two versions whose mirror failed, and a `manual` one whose first version has no
 * document (`LOST_PRESET`). `presetsOff()` is the server before the area is switched on: a bare
 * 404, with no `code`, on every path (plan N5). The writes (copy, edit, send again) answer as
 * `writes.go` does: 428 with no `Idempotency-Key`, 409 `version_moved` and `not_manual`, and 503
 * `writes_not_configured` after `writesOff()`. The checks run in Go's order: the key (428) first, then the
 * body (R7: 422 `key_refused` and `invalid_input`), then the preset (404), `not_manual` and
 * `version_moved`. `writePending()` answers 503 `preset_write_pending`, and `mirrorShows()` answers a send
 * again with a refusal (409 `mirror_off`, `not_configured`, `instance_changed`) or an outcome (`busy`,
 * `pending`, `failed`). One key with one body answers the first result again. A body that is not a
 * JSON object is 422 `invalid_input`, and `keyReused()` answers each copy and edit 422
 * `idempotency_key_reused`, as the activity does for one key sent with another body.
 * Key sets are tested against `test-fixtures/filament/preset-wire.json`.
 */

const BASE = '/api/v1/filament/presets'

/** The preset whose version 1 has no document: its diff is 409 `document_missing`. */
export const LOST_PRESET = 4

type Doc = Record<string, unknown>
interface Seeded {
  row: PresetRow
  versions: { version: PresetVersion; document: Doc | null }[]
}

const SHA = (n: number) => String(n).padStart(64, 'a')

function seeded(): Seeded[] {
  const row = (id: number, source: PresetSource, over: Partial<PresetRow>): PresetRow => ({
    id,
    ref: `sb-${id}`,
    name: `Preset ${id}`,
    source,
    version: 1,
    type_id: null,
    printer_model: 'Bambu Lab H2C',
    nozzle_diameter: '0.4',
    filament_type: 'PLA',
    filament_colour: null,
    compatible_printers: ['Bambu Lab H2C 0.4 nozzle'],
    sha256: SHA(id),
    bb_preset_id: null,
    bb_instance: null,
    mirror: 'sent',
    mirror_error: null,
    mirror_error_at: null,
    used_by_types: 0,
    used_by_spools: 0,
    created_at: '2026-11-26T10:00:00Z',
    updated_at: '2026-11-26T12:30:00.123456Z',
    ...over,
  })
  const version = (n: number, made_by: string | null, sha: string): PresetVersion => ({
    version: n,
    sha256: sha,
    made_by,
    made_at: '2026-11-26T12:30:00.123456Z',
    fetch_run_id: null,
    bb_updated_at: null,
  })
  return [
    {
      row: row(1, '3dfp', {
        name: 'Bambu PLA Basic @H2C 0.4',
        type_id: T.JADE,
        filament_colour: '#FFFFFF',
        bb_preset_id: 41,
        bb_instance: 'bambuddy.example',
        used_by_types: 1,
        used_by_spools: 2,
      }),
      versions: [{ version: version(1, 'profile_fetch', SHA(1)), document: { name: 'Bambu PLA Basic @H2C 0.4', nozzle_temperature: ['220'] } }],
    },
    {
      row: row(2, 'bambuddy', { name: 'Bambuddy PETG HF', filament_type: 'PETG', bb_preset_id: 12, bb_instance: 'bambuddy.example', mirror: 'bambuddy_owned' }),
      versions: [{ version: version(1, 'bambuddy_pull', SHA(2)), document: { name: 'Bambuddy PETG HF', nozzle_temperature: ['250'] } }],
    },
    {
      row: row(3, 'manual', {
        name: 'My PLA, slower',
        version: 2,
        sha256: SHA(32),
        mirror: 'failed',
        mirror_error: 'Bambuddy did not answer. The next pull sends it again.',
        mirror_error_at: '2026-11-26T12:31:00.000000Z',
      }),
      versions: [
        { version: version(1, 'copy', SHA(31)), document: { name: 'My PLA, slower', nozzle_temperature: ['215'], filament_max_volumetric_speed: ['12'] } },
        { version: version(2, 'edit', SHA(32)), document: { name: 'My PLA, slower', nozzle_temperature: ['220'], filament_max_volumetric_speed: ['10'] } },
      ],
    },
    {
      row: row(LOST_PRESET, 'manual', { name: 'My PETG, lost', filament_type: 'PETG', version: 2, sha256: SHA(42), mirror: 'pending' }),
      versions: [
        { version: version(1, 'copy', SHA(41)), document: null },
        { version: version(2, 'edit', SHA(42)), document: { name: 'My PETG, lost', nozzle_temperature: ['245'] } },
      ],
    },
    {
      row: row(5, '3dfp', { name: 'Zed PETG HF @H2C 0.4', filament_type: 'PETG', type_id: T.ORANGE, filament_colour: '#FF6A13' }),
      versions: [{ version: version(1, 'profile_fetch', SHA(5)), document: { name: 'Zed PETG HF @H2C 0.4', nozzle_temperature: ['250'] } }],
    },
  ]
}

type MirrorForced = 'mirror_off' | 'not_configured' | 'instance_changed' | 'busy' | 'pending' | 'failed'

let presets = seeded()
let off = false
let noWrites = false
let pendingWrites = false
let reusedKeys = false
let mirrorForced: MirrorForced | null = null
let answered = new Map<string, unknown>()

/** The server before the presets area is switched on: a bare 404 with no `code` on every preset path. */
export function presetsOff(on = true) {
  off = on
}

/** The server with the area on but no writes (no Temporal): 503 `writes_not_configured` on each write. */
export function writesOff(on = true) {
  noWrites = on
}

/** The server whose write does not finish in time: 503 `preset_write_pending` on each write. */
export function writePending(on = true) {
  pendingWrites = on
}

/** The ledger that holds each key with another body: 422 `idempotency_key_reused` on each copy and edit. */
export function keyReused(on = true) {
  reusedKeys = on
}

/** What a send again answers: a refusal or an outcome of the mirror. `null` is the plain answer. */
export function mirrorShows(forced: MirrorForced | null) {
  mirrorForced = forced
}

export function reset() {
  presets = seeded()
  off = false
  noWrites = false
  pendingWrites = false
  reusedKeys = false
  mirrorForced = null
  answered = new Map()
}

const problem = (status: number, code: string, detail: string, extra: Record<string, unknown> = {}) =>
  HttpResponse.json({ type: 'about:blank', title: detail, status, detail, code, ...extra }, { status, headers: { 'Content-Type': 'application/problem+json' } })

const KEY_REQUIRED = 'https://scadbuddy.dev/problems/crud/idempotency-key-required'
const keyRequired = () =>
  HttpResponse.json(
    { type: KEY_REQUIRED, title: 'Precondition Required', status: 428, detail: 'This write needs an Idempotency-Key header. Nothing was done.' },
    { status: 428, headers: { 'Content-Type': 'application/problem+json' } },
  )

const bare404 = () => new HttpResponse(null, { status: 404 })

const find = (raw: string | readonly string[] | undefined) => presets.find((one) => String(one.row.id) === String(raw))

const detailOf = (one: Seeded): PresetDetail => {
  const current = one.versions.find((v) => v.version.version === one.row.version)
  return {
    ...one.row,
    versions: [...one.versions].reverse().map((v) => v.version),
    document: current?.document ?? null,
    document_error: current?.document ? null : 'document_missing',
  }
}

function diffOf(one: Seeded, from: number, to: number): PresetDiff | Response {
  const a = one.versions.find((v) => v.version.version === from)
  const b = one.versions.find((v) => v.version.version === to)
  if (!a || !b) return problem(404, 'version_not_found', 'no such version')
  if (!a.document || !b.document) return problem(409, 'document_missing', 'the document of a version is not in the store')
  const keys = [...new Set([...Object.keys(a.document), ...Object.keys(b.document)])].sort()
  const changes: PresetKeyChange[] = keys
    .filter((key) => JSON.stringify(a.document![key]) !== JSON.stringify(b.document![key]))
    .map((key) => ({ key, before: a.document![key] ?? null, after: b.document![key] ?? null }))
  return { preset_id: one.row.id, from_version: from, to_version: to, changes }
}

const NOW = '2026-11-27T09:00:00.000000Z'

const KEY_PATTERN = /^[A-Za-z0-9_.-]{1,128}$/
const MAX_EDIT_KEYS = 500
const CREDENTIAL_KEYS = new Set(['printhost_apikey', 'printhost_password', 'printhost_user'])
const CREDENTIAL_PARTS = ['apikey', 'password', 'secret', 'token']
/** `IsCredential` of Go: U+0130 is spelled as Python lowers it (an "i" and a combining dot above) before the lowercase. */
const isCredential = (key: string) => {
  const lowered = key.replaceAll('\u0130', 'i\u0307').toLowerCase()
  return CREDENTIAL_KEYS.has(lowered) || CREDENTIAL_PARTS.some((part) => lowered.includes(part))
}
const shown = (key: string) => (Array.from(key).length <= 64 ? key : `${Array.from(key).slice(0, 64).join('')}…`)

const keyRefused = (key: string, detail = `the key ${shown(key)} cannot be set or unset`) => problem(422, 'key_refused', detail, { key: shown(key) })
const invalid = (detail: string) => problem(422, 'invalid_input', detail)

/** `presetID` of Go: a positive integer in its plain spelling that fits an int64. */
const presetIdOf = (raw: string | readonly string[] | undefined) =>
  typeof raw === 'string' && /^[1-9][0-9]*$/.test(raw) && BigInt(raw) <= 9223372036854775807n

const MAX_KEY_CHARS = 200

/** What every write checks first, in the order of `begin` in Go: the area, the path id, the writes and (keyed) the key. */
function begin(request: Request, id: string | readonly string[] | undefined, keyed: boolean): Response | { key: string } {
  if (off) return bare404()
  if (!presetIdOf(id)) return problem(404, 'preset_not_found', 'no such preset')
  if (noWrites) return problem(503, 'writes_not_configured', 'Preset writes are not set up on this server.')
  const key = request.headers.get('Idempotency-Key')
  if (!keyed) return { key: key ?? '' }
  if (key === null) return keyRequired()
  const chars = Array.from(key).length
  if (chars < 1 || chars > MAX_KEY_CHARS) return invalid(`the Idempotency-Key is 1 to ${MAX_KEY_CHARS} characters`)
  return { key }
}

/** The body as `begin` in Go reads it: a JSON object, else 422 `invalid_input`. */
async function bodyOf<T>(request: Request): Promise<T | Response> {
  let body: unknown
  try {
    body = JSON.parse(await request.text())
  } catch {
    body = undefined
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) return invalid('the body is not a JSON object')
  return body as T
}

/** The write that started and did not finish: it is checked after the body and before the activity. */
const pendingAnswer = () => (pendingWrites ? problem(503, 'preset_write_pending', 'The write has not finished yet.') : null)

const KEY_REUSED = 'https://scadbuddy.dev/problems/crud/idempotency-key-reused'
/** The activity's ledger answers first (P17), before it reads the preset. */
const reusedAnswer = () =>
  reusedKeys ? problem(422, 'idempotency_key_reused', 'This Idempotency-Key was used for another request.', { type: KEY_REUSED }) : null

/** `nameOf`: a string of 1 to 200 characters once stripped. */
function nameRefusal(name: unknown, required: boolean): Response | null {
  if (name === undefined || name === null) return required ? invalid('name: the field is required') : null
  if (typeof name !== 'string') return invalid('name: not a string')
  const length = Array.from(name.trim()).length
  return length < 1 || length > 200 ? invalid('name: 1 to 200 characters') : null
}

/** The body of an edit as `editFields` checks it (R7), in its order. */
function editRefusal(body: PresetEdit): Response | null {
  const set = body.set as unknown
  const unset = body.unset as unknown
  const setKeys = set && typeof set === 'object' && !Array.isArray(set) ? Object.keys(set) : []
  const unsetKeys = Array.isArray(unset) ? unset.filter((item): item is string => typeof item === 'string') : []
  for (const field of [...setKeys, ...unsetKeys]) {
    if (!KEY_PATTERN.test(field) || field === 'name' || isCredential(field)) return keyRefused(field)
  }
  if (!Number.isInteger(body.expected_version) || body.expected_version < 1) return invalid('expected_version: the field is required, as an integer')
  const nameProblem = nameRefusal(body.name, false)
  if (nameProblem) return nameProblem
  if (body.set !== undefined) {
    if (!set || typeof set !== 'object' || Array.isArray(set)) return invalid('set: not an object')
    for (const [field, value] of Object.entries(set)) {
      const ok = typeof value === 'string' || (Array.isArray(value) && value.every((item) => typeof item === 'string'))
      if (!ok) return invalid(`set.${shown(field)}: not a string or a list of strings`)
    }
  }
  if (body.unset !== undefined) {
    if (!Array.isArray(unset)) return invalid('unset: not a list')
    if (unset.some((item) => typeof item !== 'string')) return invalid('unset: not a list of strings')
  }
  if (setKeys.length + unsetKeys.length > MAX_EDIT_KEYS) return invalid(`an edit sets and unsets at most ${MAX_EDIT_KEYS} keys`)
  const both = unsetKeys.filter((field) => setKeys.includes(field)).sort()
  if (both.length > 0) return keyRefused(both[0]!, `the key ${shown(both[0]!)} is both set and unset`)
  if (body.name === undefined && setKeys.length === 0 && unsetKeys.length === 0) return invalid('an edit needs a name, a key to set or a key to unset')
  return null
}

function addVersion(one: Seeded, document: Doc, madeBy: string) {
  const version = one.row.version + 1
  const sha = SHA(one.row.id * 100 + version)
  one.versions.push({ version: { version, sha256: sha, made_by: madeBy, made_at: NOW, fetch_run_id: null, bb_updated_at: null }, document })
  one.row = { ...one.row, version, sha256: sha, updated_at: NOW, mirror: 'sent', mirror_error: null, mirror_error_at: null }
}

function applyEdit(one: Seeded, body: PresetEdit): { changed: boolean } {
  const current = one.versions.find((v) => v.version.version === one.row.version)
  const document: Doc = { ...(current?.document ?? {}) }
  if (body.name !== undefined) document.name = body.name.trim()
  for (const [key, value] of Object.entries(body.set ?? {})) document[key] = value
  for (const key of body.unset ?? []) delete document[key]
  const name = body.name?.trim() ?? one.row.name
  if (JSON.stringify(document) === JSON.stringify(current?.document ?? {}) && name === one.row.name) return { changed: false }
  addVersion(one, document, 'edit')
  one.row = { ...one.row, name }
  return { changed: true }
}

/** Once for each key and body: a second send of both answers the first result. */
function once<T>(op: string, id: number, key: string, body: unknown, make: () => T): T {
  const at = `${op}:${id}:${key}:${JSON.stringify(body)}`
  if (!answered.has(at)) answered.set(at, make())
  return answered.get(at) as T
}

export const handlers = [
  http.post(`${BASE}/:id/copy`, async ({ request, params }) => {
    const began = begin(request, params.id, true)
    if (began instanceof Response) return began
    const body = await bodyOf<{ name?: string }>(request)
    if (body instanceof Response) return body
    const refusal = nameRefusal(body.name, true)
    if (refusal) return refusal
    const name = (body.name as string).trim()
    const late = pendingAnswer() ?? reusedAnswer()
    if (late) return late
    const one = find(params.id)
    if (!one) return problem(404, 'preset_not_found', 'no such preset')
    const made = once('copy', one.row.id, began.key, body, () => {
      const id = Math.max(...presets.map((p) => p.row.id)) + 1
      const current = one.versions.find((v) => v.version.version === one.row.version)
      const document: Doc = { ...(current?.document ?? {}), name }
      const copy: Seeded = {
        row: { ...one.row, id, ref: `sb-${id}`, name, source: 'manual', version: 1, sha256: SHA(id), bb_preset_id: null, bb_instance: null, mirror: 'sent', mirror_error: null, mirror_error_at: null, used_by_types: 0, used_by_spools: 0, created_at: NOW, updated_at: NOW },
        versions: [{ version: { version: 1, sha256: SHA(id), made_by: 'copy', made_at: NOW, fetch_run_id: null, bb_updated_at: null }, document }],
      }
      presets.push(copy)
      return copy
    })
    return HttpResponse.json(detailOf(made), { status: 201 })
  }),
  http.patch(`${BASE}/:id`, async ({ request, params }) => {
    const began = begin(request, params.id, true)
    if (began instanceof Response) return began
    const body = await bodyOf<PresetEdit>(request)
    if (body instanceof Response) return body
    const refusal = editRefusal(body)
    if (refusal) return refusal
    const late = pendingAnswer() ?? reusedAnswer()
    if (late) return late
    const one = find(params.id)
    if (!one) return problem(404, 'preset_not_found', 'no such preset')
    if (one.row.source !== 'manual') return problem(409, 'not_manual', 'only a manual preset can be edited')
    const result = once('edit', one.row.id, began.key, body, () => {
      if (body.expected_version !== one.row.version) return { moved: one.row.version }
      return applyEdit(one, body)
    })
    if ('moved' in result) return problem(409, 'version_moved', 'another edit came first', { current_version: result.moved })
    return HttpResponse.json({ changed: result.changed, preset: detailOf(one) })
  }),
  http.post(`${BASE}/:id/mirror`, ({ request, params }) => {
    const began = begin(request, params.id, false)
    if (began instanceof Response) return began
    const late = pendingAnswer()
    if (late) return late
    const one = find(params.id)
    if (!one) return problem(404, 'preset_not_found', 'no such preset')
    if (one.row.source === 'bambuddy') return problem(409, 'not_mirrored', 'Bambuddy owns this preset')
    if (mirrorForced === 'mirror_off') return problem(409, 'mirror_off', 'the filament preset mirror is off')
    if (mirrorForced === 'not_configured') return problem(409, 'not_configured', 'no Bambuddy is configured')
    if (mirrorForced === 'instance_changed') return problem(409, 'instance_changed', 'the Bambuddy URL changed: reset the links before a send')
    if (mirrorForced === 'failed') {
      one.row = { ...one.row, mirror: 'failed', mirror_error: 'Bambuddy did not answer. The next pull sends it again.', mirror_error_at: NOW }
      return HttpResponse.json({ outcome: 'failed', preset: one.row })
    }
    if (mirrorForced === 'busy' || mirrorForced === 'pending') return HttpResponse.json({ outcome: mirrorForced, preset: one.row })
    if (one.row.mirror === 'sent') return HttpResponse.json({ outcome: 'in_sync', preset: one.row })
    one.row = { ...one.row, mirror: 'sent', mirror_error: null, mirror_error_at: null }
    return HttpResponse.json({ outcome: 'sent', preset: one.row })
  }),
  http.get(BASE, ({ request }) => {
    if (off) return bare404()
    const url = new URL(request.url)
    const source = url.searchParams.get('source')
    const typeId = url.searchParams.get('type_id')
    return HttpResponse.json(
      presets
        .map((one) => one.row)
        .filter((row) => (!source || row.source === source) && (!typeId || row.type_id === typeId))
        .sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()) || a.id - b.id),
    )
  }),
  http.get(`${BASE}/:id`, ({ params }) => {
    if (off) return bare404()
    const one = find(params.id)
    return one ? HttpResponse.json(detailOf(one)) : problem(404, 'preset_not_found', 'no such preset')
  }),
  http.get(`${BASE}/:id/diff`, ({ params, request }) => {
    if (off) return bare404()
    const one = find(params.id)
    if (!one) return problem(404, 'preset_not_found', 'no such preset')
    const url = new URL(request.url)
    const to = Number(url.searchParams.get('to_version') ?? one.row.version)
    const from = Number(url.searchParams.get('from_version') ?? Math.max(1, to - 1))
    const diff = diffOf(one, from, to)
    return diff instanceof Response ? diff : HttpResponse.json(diff)
  }),
]
