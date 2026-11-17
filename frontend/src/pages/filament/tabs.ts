/**
 * #6262 — the Filament section's tabs, in spec §10's order
 * (`docs/superpowers/specs/2026-11-14-filament-management-design.md`). Suppliers,
 * Invoices, Metrics and Shopping list are listed but not available until #6266 and #6269
 * build them (plan 2026-11-15 Decision 10): the section shows only the available ones.
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
  tab('suppliers', 'Suppliers', false),
  tab('invoices', 'Invoices', false),
  tab('locations', 'Locations', true),
  tab('catalog', 'Catalog', true),
  tab('metrics', 'Metrics', false),
  tab('shopping-list', 'Shopping list', false),
  tab('ledger', 'Ledger', true),
]

/** Where `/filament` opens. */
export const FILAMENT_HOME = '/filament/spools'
