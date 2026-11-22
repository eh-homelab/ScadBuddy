/**
 * #6869 — every filament entity's page, from one place, so a route change moves every link
 * (plan `docs/superpowers/plans/2026-11-15-filament-ui.md`, R-6869-2). The paths are
 * relative router paths: a link stays on the host the user is on (R-6869-3). An entity with
 * no page of its own links to the list that shows it: a location to the spools filtered to
 * it, a catalog entry to its catalog, an invoice line and a supplier product
 * to their row on the page that holds them (an anchor `useHashTarget` scrolls to). A shopping
 * item is a row of the one shopping list, and a printer's page is Settings → Printing's list.
 * `test-fixtures/filament/ui-paths.json` pins these; the agent's copy
 * (`agent/src/tools/filamentLinks.ts`) is tested against the same vectors.
 */
import type { CatalogKind } from '../api/filament'

const BASE = '/filament'
const at = (list: string, id: string | number) => `${list}/${encodeURIComponent(String(id))}`
const query = (path: string, params: Record<string, string>) => `${path}?${new URLSearchParams(params).toString()}`

const spools = `${BASE}/spools`
const types = `${BASE}/types`
const suppliers = `${BASE}/suppliers`
const invoices = `${BASE}/invoices`
const shoppingList = `${BASE}/shopping-list`
const catalog = `${BASE}/catalog`

/** The anchor ids the pages give an invoice line and a supplier product. */
export const anchors = {
  invoiceLine: (lineId: string) => `line-${lineId}`,
  supplierProduct: (productId: string) => `product-${productId}`,
}

const anchored = (path: string, anchor: string) => `${path}#${encodeURIComponent(anchor)}`

export const filamentLinks = {
  spools,
  newSpool: `${spools}/new`,
  spool: (id: string) => at(spools, id),
  types,
  newType: `${types}/new`,
  type: (id: string) => at(types, id),
  suppliers,
  supplier: (id: string) => at(suppliers, id),
  /** A supplier's product or price sheet row: both live on the supplier's page. */
  supplierProduct: (supplierId: string, productId: string) => anchored(at(suppliers, supplierId), anchors.supplierProduct(productId)),
  invoices,
  /** A supplier's invoices (the Invoices tab's `supplier` filter). */
  supplierInvoices: (supplierId: string) => query(invoices, { supplier: supplierId }),
  invoice: (id: string) => at(invoices, id),
  invoiceLine: (invoiceId: string, lineId: string) => anchored(at(invoices, invoiceId), anchors.invoiceLine(lineId)),
  locations: `${BASE}/locations`,
  /** A place has no page: the spools in it (the Spools tab's `place` filter, by id, so two places of one name stay apart). */
  location: (id: string) => query(spools, { place: id }),
  /** A catalog entry's list (the Catalog tab opens on materials). */
  catalog: (kind: CatalogKind) => (kind === 'materials' ? catalog : query(catalog, { kind })),
  shoppingList,
  labels: (ids: readonly string[]) => `${BASE}/labels?ids=${ids.map(encodeURIComponent).join(',')}`,
  /** The printers have no page of their own: Settings → Printing's list. */
  printers: '/settings/printing#printers',
} as const
