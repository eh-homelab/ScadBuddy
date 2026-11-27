import { filamentLinks } from './filamentLinks'

/**
 * #8372 — a scanned code that names no ScadBuddy thing searches the spools. 8 to 14 digits
 * (an EAN, a UPC or a GTIN) search the `gtin` property; any other text is a plain search.
 */
export function scanSearchQuery(text: string): string {
  const trimmed = text.trim()
  return /^\d{8,14}$/.test(trimmed) ? `gtin:${trimmed}` : trimmed
}

/** The Spools tab, searching for the code. `from=scan` makes the tab open a single match. */
export function scanSearchPath(text: string): string {
  return `${filamentLinks.spools}?q=${encodeURIComponent(scanSearchQuery(text))}&from=scan`
}

/** Whether the text has the shape of a ScadBuddy code, such as `S-142`, `L-4` or `K-3`. */
export function isScadBuddyCode(text: string): boolean {
  return /^[a-z]-?\d+$/i.test(text.trim())
}

/**
 * #8372 D11: whether a text that names no thing becomes a spool search. A text whose last path
 * segment has the ScadBuddy code shape (`L-99`, or a label URL such as `…/x/S-5`) keeps its
 * "not found" answer, and so does a ScadBuddy QR text (`scadbuddy:…`). Any other text, such as
 * a maker's barcode, becomes a search.
 */
export function searchesSpools(text: string): boolean {
  const trimmed = text.trim()
  if (/^scadbuddy:/i.test(trimmed)) return false
  const last = trimmed.split(/[?#]/)[0]!.split('/').filter(Boolean).pop() ?? ''
  return !isScadBuddyCode(last)
}
