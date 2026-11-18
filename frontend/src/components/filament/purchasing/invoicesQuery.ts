/**
 * #6266 — the Invoices tab's view: a status and a supplier, kept in the address. What a
 * browser remembers (the view, behind "Remember my view", and the last supplier an invoice
 * was added for) lives in `localStorage`; storage that throws (private mode, a sandboxed
 * frame) leaves the page working without it.
 */
export type InvoiceStatusFilter = 'all' | 'draft' | 'confirmed'

export interface InvoicesQuery {
  status: InvoiceStatusFilter
  supplier?: string
}

const VIEW_KEY = 'scadbuddy.filament.invoices.view'
const SUPPLIER_KEY = 'scadbuddy.filament.invoices.lastSupplier'

export function readQuery(params: URLSearchParams): InvoicesQuery {
  const status = params.get('status')
  const supplier = params.get('supplier')
  return { status: status === 'draft' || status === 'confirmed' ? status : 'all', ...(supplier ? { supplier } : {}) }
}

/** Only what differs from the default view, so the plain tab has a plain address. */
export function writeQuery(query: InvoicesQuery): URLSearchParams {
  const params = new URLSearchParams()
  if (query.status !== 'all') params.set('status', query.status)
  if (query.supplier) params.set('supplier', query.supplier)
  return params
}

interface Memory {
  on: boolean
  query: InvoicesQuery
}

function readMemory(): Memory | null {
  try {
    const raw = window.localStorage.getItem(VIEW_KEY)
    if (!raw) return null
    const stored = JSON.parse(raw) as { on?: unknown; query?: { status?: unknown; supplier?: unknown } }
    const params = new URLSearchParams()
    if (typeof stored.query?.status === 'string') params.set('status', stored.query.status)
    if (typeof stored.query?.supplier === 'string') params.set('supplier', stored.query.supplier)
    return { on: stored.on === true, query: readQuery(params) }
  } catch {
    return null
  }
}

function writeMemory(memory: Memory) {
  try {
    window.localStorage.setItem(VIEW_KEY, JSON.stringify(memory))
  } catch {
    // A browser that refuses storage still shows the view for this visit.
  }
}

/** Whether "Remember my view" is on. */
export const remembering = (): boolean => readMemory()?.on === true

/** The view a visit with no query opens, or `null` while "Remember my view" is off. */
export function rememberedQuery(): InvoicesQuery | null {
  const memory = readMemory()
  return memory?.on ? memory.query : null
}

/** Keeps `query` as the remembered view, when remembering is on. */
export function remember(query: InvoicesQuery) {
  if (remembering()) writeMemory({ on: true, query })
}

/** The switch: on keeps `query` from now on, off forgets it. */
export function setRemembering(on: boolean, query: InvoicesQuery) {
  writeMemory({ on, query: on ? query : { status: 'all' } })
}

export function lastSupplier(): string | null {
  try {
    return window.localStorage.getItem(SUPPLIER_KEY)
  } catch {
    return null
  }
}

export function rememberSupplier(id: string) {
  try {
    window.localStorage.setItem(SUPPLIER_KEY, id)
  } catch {
    // Not remembered; the next upload asks again.
  }
}
