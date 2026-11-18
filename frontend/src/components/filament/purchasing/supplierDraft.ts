import type { Supplier, SupplierCreate } from '../../../api/filamentPurchasing'
import { webUrlError } from './webUrl'

/** What the supplier fields hold while they are edited: text, `''` for none. */
export interface SupplierDraft {
  name: string
  website: string
  customer_number: string
  note: string
}

export const BLANK_SUPPLIER: SupplierDraft = { name: '', website: '', customer_number: '', note: '' }

export const draftOfSupplier = (supplier: Supplier): SupplierDraft => ({
  name: supplier.name,
  website: supplier.website ?? '',
  customer_number: supplier.customer_number ?? '',
  note: supplier.note ?? '',
})

/** What is wrong with the draft, or `null`; the website is checked here so a refused one never reaches the API. */
export function supplierError(draft: SupplierDraft): string | null {
  if (!draft.name.trim()) return 'Give the supplier a name.'
  return webUrlError(draft.website, 'The website')
}

/** The create body: an empty optional field is left out, text is trimmed. */
export function supplierBody(draft: SupplierDraft): SupplierCreate {
  const body: SupplierCreate = { name: draft.name.trim() }
  if (draft.website.trim()) body.website = draft.website.trim()
  if (draft.customer_number.trim()) body.customer_number = draft.customer_number.trim()
  if (draft.note.trim()) body.note = draft.note.trim()
  return body
}
