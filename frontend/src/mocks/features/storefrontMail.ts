import { HttpResponse, http } from 'msw'
import { mockSettings } from '../handlers'
import { fieldProblem, now, problem } from './orders/state'

/**
 * #5283 — `/storefront/mail`: the storefront's SMTP password, sealed under the KEK, bound
 * to the host, port and user set when it was saved, and reported only as set; and the
 * last failure of the mail server. Without the key file a save is 409 `kek_missing`, and
 * before a host and a user it is 409 `mail_connection_unset`.
 */

const base = '/api/v1'
const PROBLEMS = 'https://scadbuddy.dev/problems/storefront/'

let passwordFor: string | null = null
let kek = true
let serverError: { text: string; at: string } | null = null

export const setMockMailKek = (present: boolean) => {
  kek = present
}

export const setMockMailServerError = (text: string | null) => {
  serverError = text === null ? null : { text, at: now() }
}

export function reset(): void {
  passwordFor = null
  kek = true
  serverError = null
}

export const handlers = [
  http.get(`${base}/storefront/mail`, () =>
    HttpResponse.json({
      password_set: passwordFor !== null,
      password_for: passwordFor,
      server_error: serverError?.text ?? null,
      server_error_at: serverError?.at ?? null,
    }),
  ),
  http.get(`${base}/storefront/mail/password`, () => HttpResponse.json({ set: passwordFor !== null })),
  http.put(`${base}/storefront/mail/password`, async ({ request }) => {
    const body = (await request.json()) as { password?: unknown }
    if (typeof body.password !== 'string' || body.password.length < 1 || body.password.length > 1000) {
      return fieldProblem(request, 'password', 'String should have 1 to 1000 characters')
    }
    if (!kek) return problem(request, 409, 'the storefront mail password needs SCADBUDDY_SECRET_KEY_FILE', PROBLEMS + 'kek_missing')
    const settings = mockSettings()
    if (!settings.storefront_mail_host || !settings.storefront_mail_user) {
      return problem(request, 409, 'set the SMTP host and user before the password', PROBLEMS + 'mail_connection_unset')
    }
    passwordFor = `${settings.storefront_mail_user}@${settings.storefront_mail_host}:${settings.storefront_mail_port}`
    return HttpResponse.json({ set: true })
  }),
  http.delete(`${base}/storefront/mail/password`, () => {
    passwordFor = null
    return HttpResponse.json({ set: false })
  }),
]
