import type { Client } from 'openapi-fetch'
import type { BackendClient } from './backend.js'

// #7204: the filament preset routes. Go serves them (`tools/go/core/internal/filamentpresets`), so
// `pnpm gen:api`, which exports Python's OpenAPI only, has no types for them. These types are
// written by hand from the routes' JSON, as `test-fixtures/filament/preset-wire.json` shows it.

export type PresetSource = '3dfp' | 'bambuddy' | 'manual'
export type PresetMirror = 'sent' | 'pending' | 'failed' | 'bambuddy_owned'

export interface FilamentPresetRow {
  id: number
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

export interface FilamentPresetVersion {
  version: number
  sha256: string
  made_by: string | null
  made_at: string
  fetch_run_id: string | null
  bb_updated_at: string | null
}

export interface FilamentPresetDetail extends FilamentPresetRow {
  versions: FilamentPresetVersion[]
  document: Record<string, unknown> | null
  document_error: string | null
}

export interface FilamentPresetKeyChange {
  key: string
  before: unknown
  after: unknown
}

export interface FilamentPresetDiff {
  preset_id: number
  from_version: number
  to_version: number
  changes: FilamentPresetKeyChange[]
}

export interface FilamentPresetEdited {
  changed: boolean
  preset: FilamentPresetDetail | null
}

export interface FilamentPresetMirrorOutcome {
  outcome: string
  preset: FilamentPresetRow
}

export interface FilamentPresetEdit {
  expected_version: number
  name?: string
  set?: Record<string, string | string[]>
  unset?: string[]
}

type Problem = { code?: string; detail?: string; title?: string; current_version?: number }
type Failure = { default: { headers: Record<string, unknown>; content: { 'application/problem+json': Problem } } }

export interface FilamentPresetPaths {
  '/api/v1/filament/presets': {
    get: {
      parameters: { query?: { source?: PresetSource; type_id?: string }; header?: never; path?: never; cookie?: never }
      requestBody?: never
      responses: { 200: { headers: Record<string, unknown>; content: { 'application/json': FilamentPresetRow[] } } } & Failure
    }
  }
  '/api/v1/filament/presets/{preset_id}': {
    get: {
      parameters: { query?: never; header?: never; path: { preset_id: number }; cookie?: never }
      requestBody?: never
      responses: { 200: { headers: Record<string, unknown>; content: { 'application/json': FilamentPresetDetail } } } & Failure
    }
    patch: {
      parameters: { query?: never; header: { 'Idempotency-Key': string }; path: { preset_id: number }; cookie?: never }
      requestBody: { content: { 'application/json': FilamentPresetEdit } }
      responses: { 200: { headers: Record<string, unknown>; content: { 'application/json': FilamentPresetEdited } } } & Failure
    }
  }
  '/api/v1/filament/presets/{preset_id}/copy': {
    post: {
      parameters: { query?: never; header: { 'Idempotency-Key': string }; path: { preset_id: number }; cookie?: never }
      requestBody: { content: { 'application/json': { name: string } } }
      responses: { 201: { headers: Record<string, unknown>; content: { 'application/json': FilamentPresetDetail } } } & Failure
    }
  }
  '/api/v1/filament/presets/{preset_id}/mirror': {
    post: {
      parameters: { query?: never; header?: never; path: { preset_id: number }; cookie?: never }
      requestBody?: never
      responses: { 200: { headers: Record<string, unknown>; content: { 'application/json': FilamentPresetMirrorOutcome } } } & Failure
    }
  }
  '/api/v1/filament/presets/{preset_id}/diff': {
    get: {
      parameters: {
        query?: { from_version?: number; to_version?: number }
        header?: never
        path: { preset_id: number }
        cookie?: never
      }
      requestBody?: never
      responses: { 200: { headers: Record<string, unknown>; content: { 'application/json': FilamentPresetDiff } } } & Failure
    }
  }
}

/**
 * The backend client typed for these routes. It is the same client: same base URL, headers,
 * credential and fetch.
 */
export const filamentPresetClient = (backend: BackendClient): Client<FilamentPresetPaths> =>
  backend as unknown as Client<FilamentPresetPaths>
