import { filamentLinks } from '../../lib/filamentLinks'

/**
 * #6262 — the Filament section's tabs, in spec §10's order
 * (`docs/superpowers/specs/2026-11-14-filament-management-design.md`). The section shows
 * only the available ones. Suppliers, Invoices and Shopping list are #6266's; Metrics is
 * #6269's.
 */
export type TabId =
  | 'spools'
  | 'types'
  | 'tags'
  | 'suppliers'
  | 'invoices'
  | 'locations'
  | 'catalog'
  | 'metrics'
  | 'shopping-list'
  | 'ledger'

export interface FilamentTab {
  id: TabId
  title: string
  path: string
  available: boolean
}

const tab = (id: TabId, title: string, available: boolean): FilamentTab => ({ id, title, path: `/filament/${id}`, available })

export const FILAMENT_TABS: readonly FilamentTab[] = [
  tab('spools', 'Spools', true),
  tab('types', 'Types', true),
  tab('tags', 'Tags', true),
  tab('suppliers', 'Suppliers', true),
  tab('invoices', 'Invoices', true),
  tab('locations', 'Locations', true),
  tab('catalog', 'Catalog', true),
  tab('metrics', 'Metrics', true),
  tab('shopping-list', 'Shopping list', true),
  tab('ledger', 'Ledger', true),
]

/** Where `/filament` opens. */
export const FILAMENT_HOME = filamentLinks.spools
