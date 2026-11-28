import { ApiError, request } from './client'

/**
 * #7204 — the filament preset routes. Go serves them (`tools/go/core/internal/filamentpresets`),
 * so `pnpm gen:api`, which exports Python's OpenAPI only, has no types for them (plan N8). These
 * types are written by hand from the routes' JSON, as `test-fixtures/filament/preset-wire.json`
 * shows it; `agent/src/api/filamentPresets.ts` holds the same shapes.
 */

export type PresetSource = '3dfp' | 'bambuddy' | 'manual'
export type PresetMirror = 'sent' | 'pending' | 'failed' | 'bambuddy_owned'

export interface PresetRow {
  id: number
  /** `sb-<id>`. */
  ref: string
  name: string
  source: PresetSource
  version: number
  type_id: string | null
  printer_model: string | null
  nozzle_diameter: string | null
  filament_type: string | null
  filament_colour: string | null
  compatible_printers: string[]
  sha256: string
  bb_preset_id: number | null
  bb_instance: string | null
  mirror: PresetMirror
  mirror_error: string | null
  mirror_error_at: string | null
  used_by_types: number
  used_by_spools: number
  created_at: string
  updated_at: string
}

export interface PresetVersion {
  version: number
  sha256: string
  made_by: string | null
  made_at: string
  fetch_run_id: string | null
  bb_updated_at: string | null
}

export interface PresetDetail extends PresetRow {
  versions: PresetVersion[]
  document: Record<string, unknown> | null
  document_error: string | null
}

export interface PresetKeyChange {
  key: string
  before: unknown
  after: unknown
}

export interface PresetDiff {
  preset_id: number
  from_version: number
  to_version: number
  changes: PresetKeyChange[]
}

/** The list as `listPresets` gives it: `served` is false when the server has no presets area (a bare 404, plan N5). */
export interface PresetList {
  rows: PresetRow[]
  served: boolean
}

const PATH = '/filament/presets'

/** The `code` a failed call's problem carries (`preset_not_found`, `document_missing` …), or null. */
export function problemCode(cause: unknown): string | null {
  if (!(cause instanceof ApiError)) return null
  const code = (cause.problem as unknown as { code?: unknown }).code
  return typeof code === 'string' ? code : null
}

/** A 404 with no problem `code`: the server has no such route (the presets area is not switched on). */
export const routeMissing = (cause: unknown): boolean => cause instanceof ApiError && cause.status === 404 && problemCode(cause) === null

export async function listPresets(query: { source?: PresetSource; typeId?: string } = {}): Promise<PresetList> {
  const params = new URLSearchParams()
  if (query.source) params.set('source', query.source)
  if (query.typeId) params.set('type_id', query.typeId)
  const text = params.toString()
  try {
    return { rows: await request<PresetRow[]>(text ? `${PATH}?${text}` : PATH), served: true }
  } catch (cause) {
    if (routeMissing(cause)) return { rows: [], served: false }
    throw cause
  }
}

export const getPreset = (id: number): Promise<PresetDetail> => request<PresetDetail>(`${PATH}/${id}`)

export function diffPreset(id: number, from?: number, to?: number): Promise<PresetDiff> {
  const params = new URLSearchParams()
  if (from !== undefined) params.set('from_version', String(from))
  if (to !== undefined) params.set('to_version', String(to))
  const text = params.toString()
  return request<PresetDiff>(`${PATH}/${id}/diff${text ? `?${text}` : ''}`)
}

/** The body of an edit: `set` takes a text or a list of texts for each key, `unset` the keys to remove. */
export interface PresetEdit {
  expected_version: number
  name?: string
  set?: Record<string, string | string[]>
  unset?: string[]
}

export interface PresetEdited {
  changed: boolean
  preset: PresetDetail
}

export type MirrorOutcome = 'sent' | 'failed' | 'busy' | 'in_sync' | 'pending'

export interface MirrorAgainResult {
  outcome: MirrorOutcome
  preset: PresetRow
}

/**
 * The writes. A copy and an edit need an `Idempotency-Key`: one for each opened form, sent again
 * unchanged after a 503 `preset_write_pending`. A copy answers 201 with the new preset.
 */
export const copyPreset = (id: number, name: string, key: string): Promise<PresetDetail> =>
  request<PresetDetail>(`${PATH}/${id}/copy`, { method: 'POST', body: JSON.stringify({ name }), headers: { 'Idempotency-Key': key } })

/** An edit of a manual preset: a new version when something changed (`changed`), else the same one. */
export const editPreset = (id: number, body: PresetEdit, key: string): Promise<PresetEdited> =>
  request<PresetEdited>(`${PATH}/${id}`, { method: 'PATCH', body: JSON.stringify(body), headers: { 'Idempotency-Key': key } })

/** "Send again" needs no key: the server joins a send that is open. */
export const mirrorPreset = (id: number): Promise<MirrorAgainResult> => request<MirrorAgainResult>(`${PATH}/${id}/mirror`, { method: 'POST' })
