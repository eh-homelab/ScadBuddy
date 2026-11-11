import { HttpResponse, http } from 'msw'
import type { CustomerLinkState } from '../../../api/types'
import { event, lock, refused } from './orders'
import { now, PROBLEMS, problem, Refused, state } from './state'

/**
 * #5283 — an order's customer link (backend `api/orders.py` `…/customer-link`): its newest
 * link's state, never the token. A send revokes the live link and makes a new one, to `to` or
 * the customer's first email contact that is one address (the primary one first); with neither it is
 * 422 `no_email`, as the backend's.
 */

const NONE: CustomerLinkState = { live: false, created_at: null, sent_to: null, opened_at: null, revoked_at: null }
const links = new Map<string, CustomerLinkState>()

export function reset(): void {
  links.clear()
}

/** A live link on `orderId`, for a test's setup. */
export function mockCustomerLink(orderId: string, link: Partial<CustomerLinkState> = {}): CustomerLinkState {
  const made: CustomerLinkState = { live: true, created_at: now(), sent_to: 'ada@example.test', opened_at: null, revoked_at: null, ...link }
  links.set(orderId, made)
  return made
}

/** One addr-spec, as `orders/storefront/addresses.py` `check_address` takes it. */
const ADDRESS = /^[^@\s,;<>"()]+@[^@\s,;<>"()]+\.[^@\s,;<>"()]+$/

function toProblem(request: Request, slug: string, detail: string) {
  return problem(request, 422, detail, PROBLEMS + slug, { errors: [{ loc: ['body', 'to'], msg: detail }] })
}

export const handlers = [
  http.get('/api/v1/orders/:id/customer-link', ({ request, params }) => {
    try {
      const order = lock(String(params.id))
      return HttpResponse.json(links.get(order.id) ?? NONE)
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.post('/api/v1/orders/:id/customer-link', async ({ request, params }) => {
    try {
      const order = lock(String(params.id))
      if (order.source === 'storefront' && order.submitted_at == null) {
        throw new Refused('order_not_submitted', `cart ${order.id} is not yet submitted`)
      }
      const text = await request.text()
      const asked = text ? (JSON.parse(text) as { to?: string | null }).to : undefined
      const customer = state.customers.find((c) => c.id === order.customer_id)
      const emails = (customer?.contacts ?? []).filter((c) => c.kind === 'email').sort((a, b) => Number(b.primary) - Number(a.primary))
      const to = asked?.trim() ?? emails.map((c) => c.value.trim()).find((v) => ADDRESS.test(v))
      if (to === undefined) return toProblem(request, 'no_email', 'The customer has no email contact that is one address. Give the address to send the link to.')
      if (!ADDRESS.test(to)) return toProblem(request, 'email_invalid', 'Give one email address, such as ada@example.com.')
      const made = { live: true, created_at: now(), sent_to: to, opened_at: null, revoked_at: null }
      links.set(order.id, made)
      event(order, 'order.customer_link_sent', { to })
      return HttpResponse.json(made)
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.delete('/api/v1/orders/:id/customer-link', ({ request, params }) => {
    try {
      const order = lock(String(params.id))
      const link = links.get(order.id)
      if (link?.live) {
        links.set(order.id, { ...link, live: false, revoked_at: now() })
        event(order, 'order.customer_link_revoked', { count: 1 })
      }
      return HttpResponse.json(links.get(order.id) ?? NONE)
    } catch (error) {
      return refused(request, error)
    }
  }),
]
