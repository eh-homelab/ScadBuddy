import { useCallback, useMemo, useState } from 'react'
import { api } from '../../api/client'
import type { Customer, CustomerCreate } from '../../api/types'

/** The select's value for "New customer…": no customer id looks like it. */
export const NEW_CUSTOMER = '__new__'

export interface NewCustomerDraft {
  name: string
  email: string
  phone: string
}

const EMPTY_DRAFT: NewCustomerDraft = { name: '', email: '', phone: '' }

/** A choice the dialog cannot submit yet (no customer, or a new one with no name). */
export class CustomerChoiceError extends Error {}

export interface CustomerPickerState {
  /** A customer's id, `NEW_CUSTOMER`, or '' while nothing is picked. */
  value: string
  setValue: (value: string) => void
  /** The picked existing customer's id, '' while none (or a new one) is picked. */
  chosen: string
  draft: NewCustomerDraft
  setDraft: (patch: Partial<NewCustomerDraft>) => void
  /** The customers offered: those not archived, and any this picker made. */
  options: Customer[]
  /**
   * The customer the dialog submits for: the picked one, or the new one made now
   * (`POST /customers`), which then stays picked, so a retry after the order fails
   * does not make a second. Throws `CustomerChoiceError` when there is nothing to send.
   */
  resolve: () => Promise<string>
  /** Back to nothing picked and an empty draft, for a dialog opened afresh; a customer
   *  this picker made stays offered. */
  reset: () => void
}

/** The body a new customer is made with (§3.1): a name, and an email and a phone as primary contacts. */
export function newCustomerBody(draft: NewCustomerDraft): CustomerCreate {
  const contacts: NonNullable<CustomerCreate['contacts']> = []
  const email = draft.email.trim()
  const phone = draft.phone.trim()
  if (email) contacts.push({ kind: 'email', value: email, primary: true })
  if (phone) contacts.push({ kind: 'phone', value: phone, primary: true })
  return { display_name: draft.name.trim(), contacts }
}

/** The state behind a `CustomerPicker`, over the customers the dialog has read. */
export function useCustomerPicker(customers: Customer[] | undefined): CustomerPickerState {
  const [value, setValue] = useState('')
  const [draft, setDraftState] = useState<NewCustomerDraft>(EMPTY_DRAFT)
  const [made, setMade] = useState<Customer[]>([])
  const options = useMemo(() => {
    const listed = (customers ?? []).filter((c) => !c.archived_at)
    return [...listed, ...made.filter((m) => !listed.some((c) => c.id === m.id))]
  }, [customers, made])
  const setDraft = useCallback((patch: Partial<NewCustomerDraft>) => setDraftState((was) => ({ ...was, ...patch })), [])

  // `value` and `draft` are read as this render saw them: the dialog disables the picker
  // while it submits, so neither changes between the click and the call.
  const resolve = useCallback(async () => {
    if (!value) throw new CustomerChoiceError('Pick a customer, or make a new one.')
    if (value !== NEW_CUSTOMER) return value
    if (!draft.name.trim()) throw new CustomerChoiceError('Name the new customer.')
    const customer = await api.createCustomer(newCustomerBody(draft))
    setMade((was) => [...was, customer])
    setValue(customer.id)
    setDraftState(EMPTY_DRAFT)
    return customer.id
  }, [value, draft])

  const reset = useCallback(() => {
    setValue('')
    setDraftState(EMPTY_DRAFT)
  }, [])

  return { value, setValue, chosen: value === NEW_CUSTOMER ? '' : value, draft, setDraft, options, resolve, reset }
}
