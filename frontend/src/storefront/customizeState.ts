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
    const { v, values, quantity } = saved as { v?: unknown; values?: unknown; quantity?: unknown }
    if (v !== VERSION || typeof values !== 'object' || values === null) return null
    const kept: Record<string, ParamValue> = {}
    for (const [name, value] of Object.entries(values)) if (isValue(value)) kept[name] = value
    return { values: kept, quantity: typeof quantity === 'number' && Number.isInteger(quantity) && quantity >= 1 ? quantity : 1 }
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
