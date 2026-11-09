import { HttpResponse, http } from 'msw'
import { fieldProblem, problem } from './orders/state'

/**
 * #5283 — `/storefront/mail/password`: the storefront's SMTP password, sealed under the
 * KEK and reported only as set. Without the key file a save is 409 `kek_missing`.
 */

const base = '/api/v1'
const PROBLEMS = 'https://scadbuddy.dev/problems/storefront/'

let set = false
let kek = true

export const setMockMailKek = (present: boolean) => {
  kek = present
}

export function reset(): void {
  set = false
  kek = true
}

export const handlers = [
  http.get(`${base}/storefront/mail/password`, () => HttpResponse.json({ set })),
  http.put(`${base}/storefront/mail/password`, async ({ request }) => {
    const body = (await request.json()) as { password?: unknown }
    if (typeof body.password !== 'string' || body.password.length < 1 || body.password.length > 1000) {
      return fieldProblem(request, 'password', 'String should have 1 to 1000 characters')
    }
    if (!kek) return problem(request, 409, 'the storefront mail password needs SCADBUDDY_SECRET_KEY_FILE', PROBLEMS + 'kek_missing')
    set = true
    return HttpResponse.json({ set })
  }),
  http.delete(`${base}/storefront/mail/password`, () => {
    set = false
    return HttpResponse.json({ set })
  }),
]
