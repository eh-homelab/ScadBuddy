/**
 * #6266 — the Filament purchasing client for `/api/v1/filament/*` (`backend/scadbuddy/api/
 * filament_purchasing.py`): suppliers, their products and price points, invoice drafts and
 * the shopping list, typed from the generated `schema.d.ts`. A money amount is a decimal
 * string on the wire; never turn one into a number to add it (`lib/decimal.ts`).
 */
import { ApiError, API_BASE, request } from './client'
import type { components } from './schema'

type Schemas = components['schemas']

export type Supplier = Schemas['Supplier']
export type SupplierCreate = Schemas['SupplierCreate']
export type SupplierPatch = Schemas['SupplierPatch']
export type SupplierProduct = Schemas['SupplierProduct']
export type ProductCreate = Schemas['ProductCreate']
export type ProductPatch = Schemas['ProductPatch']
export type PricePoint = Schemas['PricePoint']
export type PricePointCreate = Schemas['PricePointCreate']
export type PriceSheetRow = Schemas['PriceSheetRow']
export type TypePricePoint = Schemas['TypePricePoint']
export type SupplierStats = Schemas['SupplierStats']
export type Invoice = Schemas['Invoice']
/** The spec lists a field the server always sends (it has a default) as optional; here it is required. */
export type InvoiceLine = Schemas['InvoiceLine'] & Required<Pick<Schemas['InvoiceLine'], 'spool_ids'>>
export type InvoiceDetail = Omit<Schemas['InvoiceDetail'], 'lines'> & { lines: InvoiceLine[] }
export type InvoiceLineIn = Schemas['InvoiceLineIn']
/** `shipping` and `tax` default to 0, which the spec lists as required. */
export type InvoiceCreate = Omit<Schemas['InvoiceCreate'], 'shipping' | 'tax'> & Partial<Pick<Schemas['InvoiceCreate'], 'shipping' | 'tax'>>
export type InvoicePatch = Schemas['InvoicePatch']
export type InvoiceConflict = Schemas['InvoiceConflict']
export type InvoiceStatus = Invoice['status']
export type FileRef = Schemas['FileRef']
export type ShoppingItem = Schemas['ShoppingItem']
export type ShoppingItemCreate = Schemas['ShoppingItemCreate']
export type ShoppingItemPatch = Schemas['ShoppingItemPatch']
export type ShoppingStatus = ShoppingItem['status']
export type ShoppingRemoved = Schemas['ShoppingRemoved']
export type SpoolInvoiceRef = Schemas['SpoolInvoiceRef']
export type SpoolTypeSupplier = Schemas['SpoolTypeSupplier']

const BASE = '/filament'
const seg = encodeURIComponent

function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) if (value != null) search.set(key, String(value))
  const text = search.toString()
  return text ? `?${text}` : ''
}

const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })

// -- suppliers ------------------------------------------------------------------------

export const listSuppliers = (options: { includeArchived?: boolean } = {}) =>
  request<Supplier[]>(`${BASE}/suppliers${query({ include_archived: options.includeArchived || undefined })}`)

export const getSupplier = (id: string) => request<Supplier>(`${BASE}/suppliers/${seg(id)}`)

/** 409 when an unarchived supplier has the name. */
export const createSupplier = (body: SupplierCreate) => request<Supplier>(`${BASE}/suppliers`, json('POST', body))

/** Only the fields sent change; `null` clears a nullable one; `archived: false` restores. */
export const patchSupplier = (id: string, body: SupplierPatch) =>
  request<Supplier>(`${BASE}/suppliers/${seg(id)}`, json('PATCH', body))

/** Per supplier with a confirmed invoice, archived suppliers too (`archived`). */
export const getSupplierStats = () => request<SupplierStats[]>(`${BASE}/suppliers/stats`)

export const getPriceSheet = (supplierId: string) =>
  request<PriceSheetRow[]>(`${BASE}/suppliers/${seg(supplierId)}/price-sheet`)

// -- products and prices --------------------------------------------------------------

export const listProducts = (supplierId: string, options: { includeArchived?: boolean } = {}) =>
  request<SupplierProduct[]>(
    `${BASE}/suppliers/${seg(supplierId)}/products${query({ include_archived: options.includeArchived || undefined })}`,
  )

export const createProduct = (supplierId: string, body: ProductCreate) =>
  request<SupplierProduct>(`${BASE}/suppliers/${seg(supplierId)}/products`, json('POST', body))

export const patchProduct = (id: string, body: ProductPatch) =>
  request<SupplierProduct>(`${BASE}/products/${seg(id)}`, json('PATCH', body))

export const listPrices = (productId: string) => request<PricePoint[]>(`${BASE}/products/${seg(productId)}/prices`)

/** A manual price per pack, with tax, as a decimal string. */
export const addPrice = (productId: string, body: PricePointCreate) =>
  request<PricePoint>(`${BASE}/products/${seg(productId)}/prices`, json('POST', body))

/** Only a manual point (409 for one an invoice made). */
export const deletePrice = (id: string) => request<void>(`${BASE}/prices/${seg(id)}`, { method: 'DELETE' })

/** Every price point of the type's products, newest first, archived ones marked. */
export const listTypePrices = (typeId: string) => request<TypePricePoint[]>(`${BASE}/types/${seg(typeId)}/prices`)

// -- invoices -------------------------------------------------------------------------

export const listInvoices = (options: { supplierId?: string; status?: InvoiceStatus } = {}) =>
  request<Invoice[]>(`${BASE}/invoices${query({ supplier_id: options.supplierId, status: options.status })}`)

export const getInvoice = (id: string) => request<InvoiceDetail>(`${BASE}/invoices/${seg(id)}`)

/** 409 with `existing_invoice_id` when the supplier has an invoice with the number (`invoiceConflictId`). */
export const createInvoice = (body: InvoiceCreate) => request<InvoiceDetail>(`${BASE}/invoices`, json('POST', body))

/** A confirmed invoice takes only `note`. */
export const patchInvoice = (id: string, body: InvoicePatch) =>
  request<InvoiceDetail>(`${BASE}/invoices/${seg(id)}`, json('PATCH', body))

/** The whole list of a draft's lines, in order. */
export const putInvoiceLines = (id: string, lines: InvoiceLineIn[]) =>
  request<InvoiceDetail>(`${BASE}/invoices/${seg(id)}/lines`, json('PUT', lines))

/** A draft only. */
export const deleteInvoice = (id: string) => request<void>(`${BASE}/invoices/${seg(id)}`, { method: 'DELETE' })

/** A PDF or a photo, as one multipart `file` part; a second upload replaces the first. */
export function uploadInvoiceFile(id: string, file: File): Promise<FileRef> {
  const body = new FormData()
  body.append('file', file)
  return request<FileRef>(`${BASE}/invoices/${seg(id)}/file`, { method: 'POST', body })
}

/** The file's address under the API (`API_BASE`), for a link or an `<img>`: no host in it. */
export const invoiceFileUrl = (id: string) => `${API_BASE}${BASE}/invoices/${seg(id)}/file`

/** The file's bytes, for `downloadBlob`: the app may run in a sandboxed frame, where an anchor's `download` is dropped. */
export async function getInvoiceFileBlob(id: string): Promise<Blob> {
  const response = await fetch(invoiceFileUrl(id))
  if (!response.ok) throw new Error('The file could not be loaded. Try again.')
  return response.blob()
}

/** Turns the draft into spools; 409, with nothing written, names what is wrong. */
export const confirmInvoice = (id: string) => request<InvoiceDetail>(`${BASE}/invoices/${seg(id)}/confirm`, { method: 'POST' })

/**
 * #6267 — starts the assistant session that reads an invoice's file and fills its draft (the
 * agent's `POST /api/v1/ai/filament/invoice-import`). The refusals are `ApiError`s: 409 with
 * `type: 'import_running'` and the `session_id` that already does it, 409 `confirmed`, 422 (no
 * file, or one it cannot read: `detail` is the sentence for the person) and 502 `upstream_error`.
 */
export const startInvoiceImport = (invoiceId: string) =>
  request<{ session_id: string }>('/ai/filament/invoice-import', json('POST', { invoice_id: invoiceId }))

/** The session an `import_running` 409 names, else `null`. */
export function runningImportId(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const { type, session_id } = error.problem as { type?: string; session_id?: unknown }
  return type === 'import_running' && typeof session_id === 'string' ? session_id : null
}

/**
 * The invoice that has the number a create or a change named: the 409's
 * `existing_invoice_id` (absent on a confirmed invoice's 409), else `null`.
 */
export function invoiceConflictId(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const id = (error.problem as Partial<InvoiceConflict>).existing_invoice_id
  return typeof id === 'string' ? id : null
}

// -- the shopping list ----------------------------------------------------------------

export const listShopping = (options: { status?: ShoppingStatus } = {}) =>
  request<ShoppingItem[]>(`${BASE}/shopping-list${query({ status: options.status })}`)

export const addShopping = (body: ShoppingItemCreate) => request<ShoppingItem>(`${BASE}/shopping-list`, json('POST', body))

export const patchShopping = (id: string, body: ShoppingItemPatch) =>
  request<ShoppingItem>(`${BASE}/shopping-list/${seg(id)}`, json('PATCH', body))

export const removeShopping = (id: string) => request<void>(`${BASE}/shopping-list/${seg(id)}`, { method: 'DELETE' })

/** Every item, or only the purchased ones. */
export const clearShopping = (options: { purchasedOnly?: boolean } = {}) =>
  request<ShoppingRemoved>(`${BASE}/shopping-list${query({ purchased_only: options.purchasedOnly || undefined })}`, { method: 'DELETE' })
