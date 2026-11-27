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
