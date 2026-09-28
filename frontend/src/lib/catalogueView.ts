import type { CatalogueView } from './catalogueQuery'

/**
 * #278 — the catalogue view (cards or list) this browser last chose, restored when the
 * URL names none. Storage that throws (private mode, blocked site data, Bambuddy's
 * sandboxed iframe) keeps the choice in memory for this page only.
 */
export const CATALOGUE_VIEW_KEY = 'scadbuddy.catalogue.view'

/** The choice made on this page when storage would not take it; wins over storage. */
let fallback: CatalogueView | null = null

function asView(value: string | null): CatalogueView | null {
  return value === 'cards' || value === 'list' ? value : null
}

export function readStoredView(): CatalogueView | null {
  if (fallback) return fallback
  try {
    return asView(window.localStorage.getItem(CATALOGUE_VIEW_KEY))
  } catch {
    return null
  }
}

export function storeView(view: CatalogueView) {
  try {
    window.localStorage.setItem(CATALOGUE_VIEW_KEY, view)
    fallback = null
  } catch {
    // Kept in memory for this page only, even where storage can still be read.
    fallback = view
  }
}

/** For tests: forget the in-memory choice. */
export function resetStoredView() {
  fallback = null
}
