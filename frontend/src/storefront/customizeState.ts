import type { ParamValue } from './api/client'

/**
 * #6151 — what a visitor chose on one listing's page, kept in `localStorage` so leaving
 * the page and coming back loses nothing. Only the visitor's own choices are kept: the
 * page sends them again, and the shop's side reuses a render it already made of them.
 * Storage can be missing or refuse (a private window, blocked site data): every read and
 * write is guarded, and the page works without it.
 */
export interface SavedChoices {
  values: Record<string, ParamValue>
  quantity: number
  /** The filament picked for each colour parameter, by id (#6152). */
  picks?: Record<string, string>
  /** With the listing's `one_material`: the material chosen (#6152). */
  material?: string | null
}

const VERSION = 1

export function choicesKey(listingId: string): string {
  return `scadbuddy.storefront.customize.${listingId}`
}

function isValue(value: unknown): value is ParamValue {
  return typeof value === 'string' || typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))
}

/** The saved choices for `listingId`, or null when there are none or they cannot be read. */
export function loadChoices(listingId: string): SavedChoices | null {
  try {
    const text = window.localStorage.getItem(choicesKey(listingId))
    if (text === null) return null
    const saved: unknown = JSON.parse(text)
    if (typeof saved !== 'object' || saved === null) return null
    const { v, values, quantity, picks, material } = saved as {
      v?: unknown
      values?: unknown
      quantity?: unknown
      picks?: unknown
      material?: unknown
    }
    if (v !== VERSION || typeof values !== 'object' || values === null) return null
    const kept: Record<string, ParamValue> = {}
    for (const [name, value] of Object.entries(values)) if (isValue(value)) kept[name] = value
    const keptPicks: Record<string, string> = {}
    if (typeof picks === 'object' && picks !== null && !Array.isArray(picks)) {
      for (const [name, id] of Object.entries(picks)) if (typeof id === 'string') keptPicks[name] = id
    }
    return {
      values: kept,
      quantity: typeof quantity === 'number' && Number.isInteger(quantity) && quantity >= 1 ? quantity : 1,
      picks: keptPicks,
      material: typeof material === 'string' ? material : null,
    }
  } catch {
    return null
  }
}

export function saveChoices(listingId: string, choices: SavedChoices): void {
  try {
    window.localStorage.setItem(choicesKey(listingId), JSON.stringify({ v: VERSION, ...choices }))
  } catch {
    // Storage refused: the page goes on without it.
  }
}

export function clearChoices(listingId: string): void {
  try {
    window.localStorage.removeItem(choicesKey(listingId))
  } catch {
    // As above.
  }
}
