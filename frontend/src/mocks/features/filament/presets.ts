import { HttpResponse, http } from 'msw'
import type { PresetDetail, PresetDiff, PresetKeyChange, PresetRow, PresetSource, PresetVersion } from '../../../api/filamentPresets'
import { T } from './seed'

/**
 * #7204 — the preset routes (`tools/go/core/internal/filamentpresets`): the list, a preset and the
 * diff of two of its versions. Seeded with a `3dfp` preset (sent), a `bambuddy` one, a `manual`
 * one with two versions whose mirror failed, and a `manual` one whose first version has no
 * document (`LOST_PRESET`). `presetsOff()` is the server before the area is switched on: a bare
 * 404, with no `code`, on every path (plan N5). Key sets are tested against
 * `test-fixtures/filament/preset-wire.json`.
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
  ]
}

let presets = seeded()
let off = false

/** The server before the presets area is switched on: a bare 404 with no `code` on every preset path. */
export function presetsOff(on = true) {
  off = on
}

export function reset() {
  presets = seeded()
  off = false
}

const problem = (status: number, code: string, detail: string) =>
  HttpResponse.json({ type: 'about:blank', title: detail, status, detail, code }, { status, headers: { 'Content-Type': 'application/problem+json' } })

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

export const handlers = [
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
