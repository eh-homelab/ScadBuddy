import { http, HttpResponse } from 'msw'
import type { AccountOrder } from '../../storefront/api/client'
import { problem } from '../handlers'

/**
 * #5283 (spec 2026-11-02 §5.2; backend `storefront/routes.py` account routes): the public
 * page's sign-in and "Your orders". Any code of sixteen code symbols signs in (hyphens,
 * spaces and case ignored, as the CRUD side reads it); any other is 400 `sign_in_failed`,
 * one answer for every wrong code. A signed-in account has two submitted orders. Sign-out
 * ends the session. The session lasts as long as the page, as the edge's cookie would
 * for a tab.
 */

const base = '/api/store/v1/account'
const PROBLEMS = 'https://scadbuddy.dev/problems/storefront/'
/** `orders/storefront/accounts.py` `CODE_ALPHABET`: Crockford's base32 less 0 and 1. */
const CODE = /^[2-9A-HJKMNP-TV-Z]{16}$/

export const ACCOUNT_ORDERS: AccountOrder[] = [
  {
    id: 'o-2',
    number: 'ORD-2026-14',
    status: 'quoted',
    created_at: '2026-11-08T10:00:00Z',
    submitted_at: '2026-11-08T10:05:00Z',
    items: 2,
  },
  {
    id: 'o-1',
    number: 'ORD-2026-9',
    status: 'shipped',
    created_at: '2026-10-30T09:00:00Z',
    submitted_at: '2026-10-30T09:10:00Z',
    items: 1,
  },
]

let email: string | null = null

function refused(status: number, slug: string, detail: string) {
  return problem(status, status === 401 ? 'Unauthorized' : 'Bad Request', detail, { type: PROBLEMS + slug })
}

export const handlers = [
  http.post(`${base}/verify`, async ({ request }) => {
    const body = (await request.json()) as { email?: string; code?: string }
    const code = (body.code ?? '').replace(/[\s-]/g, '').toUpperCase()
    if (!body.email?.includes('@') || !CODE.test(code)) {
      return refused(400, 'sign_in_failed', 'That code did not sign you in. Check it, or ask for a new one.')
    }
    email = body.email.trim().toLowerCase()
    return HttpResponse.json({ email })
  }),
  http.post(`${base}/sign-out`, () => {
    email = null
    return new HttpResponse(null, { status: 204 })
  }),
  http.get(base, () => HttpResponse.json(email === null ? null : { email })),
  http.get(`${base}/orders`, () =>
    email === null
      ? refused(401, 'sign_in_required', 'Sign in to see your orders.')
      : HttpResponse.json({ orders: ACCOUNT_ORDERS }),
  ),
]

export function reset(): void {
  email = null
}
