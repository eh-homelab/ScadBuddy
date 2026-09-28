import type { CatalogueView } from './catalogueQuery'

/**
 * #278 — the catalogue view (cards or list) this browser last chose, restored when the
 * URL names none. Storage that throws (private mode, blocked site data, Bambuddy's
 * sandboxed iframe) keeps the choice in memory for this page only.
 */
export const CATALOGUE_VIEW_KEY = 'scadbuddy.catalogue.view'

let fallback: CatalogueView | null = null

function asView(value: string | null): CatalogueView | null {
  return value === 'cards' || value === 'list' ? value : null
}

export function readStoredView(): CatalogueView | null {
  try {
    return asView(window.localStorage.getItem(CATALOGUE_VIEW_KEY))
  } catch {
    return fallback
  }
}

export function storeView(view: CatalogueView) {
  fallback = view
  try {
    window.localStorage.setItem(CATALOGUE_VIEW_KEY, view)
  } catch {
    // Kept in memory for this page only.
  }
}

/** For tests: forget the in-memory choice. */
export function resetStoredView() {
  fallback = null
}
