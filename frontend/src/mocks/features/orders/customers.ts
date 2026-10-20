import { HttpResponse, http } from 'msw'
import type { Customer, CustomerAddress, CustomerContact, CustomerCreate, CustomerPatch } from '../../../api/types'
import { fieldProblem, newId, now, problem, state, UNKNOWN_PARAMETER } from './state'

/** `/api/v1/customers` (backend/scadbuddy/api/customers.py) over the shared store. */

const LIST_PARAMS = new Set(['q', 'include_archived', 'limit'])

function onePrimaryPerKind(contacts: CustomerCreate['contacts'] | undefined): string | null {
  const seen = new Set<string>()
  for (const contact of contacts ?? []) {
    if (!contact.primary) continue
    if (seen.has(contact.kind)) return `more than one primary ${contact.kind}`
    seen.add(contact.kind)
  }
  return null
}

function oneDefaultPerRole(addresses: CustomerCreate['addresses'] | undefined): string | null {
  for (const role of ['default_shipping', 'default_billing'] as const) {
    if ((addresses ?? []).filter((address) => address[role]).length > 1) return `more than one ${role} address`
  }
  return null
}

/** Ruling 11: a row named by `id` is kept, one without is inserted, the rest are deleted. */
function replaceRows<Row extends { id: string; position: number }, Input extends { id?: string | null }>(
  current: Row[],
  inputs: Input[],
  make: (input: Input, position: number) => Omit<Row, 'id' | 'position'>,
  prefix: string,
): Row[] | string {
  const kept: Row[] = []
  for (const [position, input] of inputs.entries()) {
    const id = typeof input.id === 'string' ? input.id : null
    if (id !== null && !current.some((row) => row.id === id)) return `${id} is not a row of this customer`
    kept.push({ ...make(input, position), id: id ?? newId(prefix), position } as Row)
  }
  return kept
}

function patch(customer: Customer, body: CustomerPatch): Customer | string {
  const primary = onePrimaryPerKind(body.contacts ?? undefined)
  if (primary) return `contacts: ${primary}`
  const defaults = oneDefaultPerRole(body.addresses ?? undefined)
  if (defaults) return `addresses: ${defaults}`
  const next: Customer = { ...customer, updated_at: now() }
  if (body.display_name !== undefined && body.display_name !== null) next.display_name = body.display_name
  if ('company' in body) next.company = typeof body.company === 'string' ? body.company : null
  if (body.notes !== undefined && body.notes !== null) next.notes = body.notes
  if (body.tags !== undefined && body.tags !== null) next.tags = body.tags
  if (body.contacts) {
    const rows = replaceRows<CustomerContact, NonNullable<CustomerPatch['contacts']>[number]>(
      customer.contacts,
      body.contacts,
      (c) => ({ kind: c.kind, value: c.value, label: c.label ?? null, primary: c.primary ?? false }),
      'k',
    )
    if (typeof rows === 'string') return `contacts: ${rows}`
    next.contacts = rows
  }
  if (body.addresses) {
    const rows = replaceRows<CustomerAddress, NonNullable<CustomerPatch['addresses']>[number]>(
      customer.addresses,
      body.addresses,
      (a) => ({
        label: a.label ?? null,
        recipient: a.recipient,
        lines: a.lines ?? [],
        locality: a.locality ?? null,
        region: a.region ?? null,
        postal_code: a.postal_code ?? null,
        country: a.country ?? null,
        phone: a.phone ?? null,
        default_shipping: a.default_shipping ?? false,
        default_billing: a.default_billing ?? false,
      }),
      'a',
    )
    if (typeof rows === 'string') return `addresses: ${rows}`
    const dropped = customer.addresses.filter((was) => !rows.some((row) => row.id === was.id)).map((row) => row.id)
    // `ON DELETE SET NULL`: an open order's pointer at a dropped address clears.
    for (const order of state.orders) {
      if (order.shipping_address_id && dropped.includes(order.shipping_address_id)) order.shipping_address_id = null
      if (order.billing_address_id && dropped.includes(order.billing_address_id)) order.billing_address_id = null
    }
    next.addresses = rows
  }
  return next
}

export const handlers = [
  http.get('/api/v1/customers', ({ request }) => {
    const sp = new URL(request.url).searchParams
    for (const name of sp.keys()) {
      if (!LIST_PARAMS.has(name)) return problem(request, 400, `unknown query parameter '${name}'`, UNKNOWN_PARAMETER)
    }
    const limit = Number(sp.get('limit') ?? '100')
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) return problem(request, 422, 'limit must be an integer from 1 to 500')
    const q = (sp.get('q') ?? '').toLowerCase()
    const archived = sp.get('include_archived') === 'true'
    const found = state.customers
      .filter((c) => archived || c.archived_at === null)
      .filter(
        (c) =>
          !q ||
          c.display_name.toLowerCase().includes(q) ||
          (c.company ?? '').toLowerCase().includes(q) ||
          c.contacts.some((k) => k.value.toLowerCase().includes(q)),
      )
      .sort((a, b) => a.display_name.localeCompare(b.display_name))
      .slice(0, limit)
    return HttpResponse.json(found)
  }),
  http.post('/api/v1/customers', async ({ request }) => {
    const body = (await request.json()) as CustomerCreate & Record<string, unknown>
    for (const key of Object.keys(body)) {
      if (!['display_name', 'company', 'notes', 'tags', 'contacts', 'addresses'].includes(key)) {
        return fieldProblem(request, key, 'Extra inputs are not permitted')
      }
    }
    if (!body.display_name) return fieldProblem(request, 'display_name', 'Field required')
    if ([...(body.contacts ?? []), ...(body.addresses ?? [])].some((row) => row.id)) {
      return fieldProblem(request, 'contacts', "a new customer's contacts and addresses have no ids yet")
    }
    const customer: Customer = {
      id: newId('c'),
      display_name: body.display_name,
      company: body.company ?? null,
      notes: body.notes ?? '',
      tags: body.tags ?? [],
      created_at: now(),
      updated_at: now(),
      archived_at: null,
      contacts: [],
      addresses: [],
    }
    const filled = patch(customer, { contacts: body.contacts ?? [], addresses: body.addresses ?? [] })
    if (typeof filled === 'string') return fieldProblem(request, filled.split(':')[0] ?? 'contacts', filled)
    state.customers.push(filled)
    return HttpResponse.json(filled, { status: 201 })
  }),
  http.get('/api/v1/customers/:id', ({ request, params }) => {
    const found = state.customers.find((c) => c.id === params.id)
    return found ? HttpResponse.json(found) : problem(request, 404, `no customer '${String(params.id)}'`)
  }),
  http.patch('/api/v1/customers/:id', async ({ request, params }) => {
    const index = state.customers.findIndex((c) => c.id === params.id)
    if (index < 0) return problem(request, 404, `no customer '${String(params.id)}'`)
    const body = (await request.json()) as CustomerPatch & Record<string, unknown>
    for (const key of Object.keys(body)) {
      if (!['display_name', 'company', 'notes', 'tags', 'contacts', 'addresses'].includes(key)) {
        return fieldProblem(request, key, 'Extra inputs are not permitted')
      }
    }
    const next = patch(state.customers[index]!, body)
    if (typeof next === 'string') return fieldProblem(request, next.split(':')[0] ?? 'contacts', next)
    state.customers[index] = next
    return HttpResponse.json(next)
  }),
  http.post('/api/v1/customers/:id/archive', ({ request, params }) => {
    const found = state.customers.find((c) => c.id === params.id)
    if (!found) return problem(request, 404, `no customer '${String(params.id)}'`)
    found.archived_at ??= now()
    found.updated_at = now()
    return HttpResponse.json(found)
  }),
]
