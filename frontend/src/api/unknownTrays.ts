import { ApiError, request } from './client'

/**
 * #7143 — the unknown-spool routes. Go serves them (`tools/go/core/internal/unknowntrays`), so
 * `pnpm gen:api`, which exports Python's OpenAPI only, has no types for them (plan G12). These
 * types are written by hand from the routes' JSON.
 */

/** A spool that may be in the tray, ranked by the server: unplaced first, then material and colour. */
export interface UnknownSpoolCandidate {
  spool_id: string
  number: number
  /** The code a label shows, `S-<number>`. */
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

/** A loaded tray that no spool is linked to. */
export interface UnknownSpoolTray {
  id: string
  printer_id: string
  printer_name: string
  ams_id: number
  tray_id: number
  /** "AMS-A slot 2". */
  slot: string
  place_id: string | null
  place_name: string | null
  place_code: string | null
  material: string | null
  colour_rgba: string | null
  remain_pct: number | null
  first_seen_at: string
  /**
   * When the report last changed this row, as the server wrote it (microseconds). Send it back
   * as it is: never parse it into a `Date`, which keeps milliseconds only (a false `tray_changed`).
   */
  seen_at: string
  candidates: UnknownSpoolCandidate[]
}

/** A spool ScadBuddy made from an RFID tag the AMS read. */
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

/** The list as `listUnknownTrays` gives it: `served` is false when the server has no unknown-spool area (a 404). */
export interface UnknownSpoolList extends UnknownSpoolTrays {
  served: boolean
}

/** The resolve body: one spool by id or by its code, the net weight, and the tray's `seen_at` as listed. */
export type UnknownSpoolResolve =
  | { spool_id: string; net_weight_g: number; seen_at: string }
  | { code: string; net_weight_g: number; seen_at: string }

/** The spool the tray now holds, and the tray's place (G6). */
export interface UnknownTrayResolved {
  id: string
  number: number
  code: string
  location_id: string | null
}

const PATH = '/filament/unknown-trays'

/** The `code` a failed call's problem carries (`tray_changed`, `code_unknown`, `tray_gone` …), or null. */
export function problemCode(cause: unknown): string | null {
  if (!(cause instanceof ApiError)) return null
  const code = (cause.problem as unknown as { code?: unknown }).code
  return typeof code === 'string' ? code : null
}

/**
 * A 404 is an empty list with `served: false`: Go has not been switched on for the area (plan G4).
 * A caller that must say "no unknown trays" tells that apart from an empty list the server gave.
 */
export async function listUnknownTrays(): Promise<UnknownSpoolList> {
  try {
    return { ...(await request<UnknownSpoolTrays>(PATH)), served: true }
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 404) return { trays: [], made_from_tags: [], served: false }
    throw cause
  }
}

/** A 404 with no problem `code`: the server has no such route (the unknown-spool area is not switched on). */
export const routeMissing = (cause: unknown): boolean => cause instanceof ApiError && cause.status === 404 && problemCode(cause) === null

export const resolveUnknownTray = (id: string, body: UnknownSpoolResolve): Promise<UnknownTrayResolved> =>
  request<UnknownTrayResolved>(`${PATH}/${encodeURIComponent(id)}/resolve`, { method: 'POST', body: JSON.stringify(body) })

export const dismissUnknownTray = (id: string): Promise<void> =>
  request<void>(`${PATH}/${encodeURIComponent(id)}/dismiss`, { method: 'POST' })
