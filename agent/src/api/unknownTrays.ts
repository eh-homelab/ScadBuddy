import type { Client } from 'openapi-fetch'
import type { BackendClient } from './backend.js'

// #7143: the unknown-spool routes. Go serves them (`tools/go/core/internal/unknowntrays`), so
// `pnpm gen:api`, which exports Python's OpenAPI only, has no types for them. These types are
// written by hand from the routes' JSON, as `frontend/src/api/unknownTrays.ts` does.

export interface UnknownSpoolCandidate {
  spool_id: string
  number: number
  code: string
  material: string | null
  colour_name: string | null
  rgba: string | null
  label_weight_g: number | null
  grams_left: number | null
  place_id: string | null
  place_name: string | null
  colour_distance: number | null
}

export interface UnknownSpoolTray {
  id: string
  printer_id: string
  printer_name: string
  ams_id: number
  tray_id: number
  slot: string
  place_id: string | null
  place_name: string | null
  place_code: string | null
  material: string | null
  colour_rgba: string | null
  remain_pct: number | null
  first_seen_at: string
  seen_at: string
  candidates: UnknownSpoolCandidate[]
}

export interface TagMadeSpool {
  spool_id: string
  number: number
  code: string
  printer_name: string
  slot: string
  made_at: string
}

export interface UnknownSpoolTrays {
  trays: UnknownSpoolTray[]
  made_from_tags: TagMadeSpool[]
}

export type UnknownSpoolResolve =
  | { spool_id: string; net_weight_g: number; seen_at: string }
  | { code: string; net_weight_g: number; seen_at: string }

export interface UnknownTrayResolved {
  id: string
  number: number
  code: string
  location_id: string | null
}

type Problem = { code?: string; detail?: string; title?: string }

export interface UnknownTrayPaths {
  '/api/v1/filament/unknown-trays': {
    get: {
      parameters: { query?: never; header?: never; path?: never; cookie?: never }
      requestBody?: never
      responses: { 200: { headers: Record<string, unknown>; content: { 'application/json': UnknownSpoolTrays } } }
    }
  }
  '/api/v1/filament/unknown-trays/{tray_id}/resolve': {
    post: {
      parameters: { query?: never; header?: never; path: { tray_id: string }; cookie?: never }
      requestBody: { content: { 'application/json': UnknownSpoolResolve } }
      responses: {
        200: { headers: Record<string, unknown>; content: { 'application/json': UnknownTrayResolved } }
        default: { headers: Record<string, unknown>; content: { 'application/problem+json': Problem } }
      }
    }
  }
}

/**
 * The backend client typed for these routes. It is the same client: same base URL, headers,
 * credential and fetch (`authored` wraps its methods, so a wrapped client keeps its headers).
 */
export const unknownTrayClient = (backend: BackendClient): Client<UnknownTrayPaths> =>
  backend as unknown as Client<UnknownTrayPaths>
