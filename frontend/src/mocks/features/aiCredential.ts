import { HttpResponse, delay, http } from 'msw'
import type { AiCredentialUpdate, AiCredentialView } from '../../api/aiCredential'

/**
 * The agent service's `/api/v1/ai/credentials` (#255, #1000; `agent/src/routes/credentials.ts`)
 * for vitest and the mocked e2e run: the same shapes, the same save rules (`planPut`), and the
 * connection test's limits as the route has them: one test at a time, and 10 s from the end of
 * one to the start of the next, refused with 429 and a `Retry-After` of at least 1 s. Like the
 * service, it keeps only the last four characters of a secret. A secret containing `bad` fails
 * its connection test.
 */

const base = '/api/v1/ai/credentials'
const COOLDOWN_MS = 10_000

const EMPTY: AiCredentialView = {
  configured: false,
  kind: null,
  base_url: null,
  last4: null,
  updated_at: null,
  usable: false,
  can_save: true,
  cannot_save_reason: null,
}

function seed(): AiCredentialView {
  return {
    ...EMPTY,
    configured: true,
    kind: 'anthropic_api_key',
    last4: 'Q7xA',
    updated_at: '2026-09-28T14:02:00Z',
    usable: true,
  }
}

const state = {
  view: seed(),
  failing: false,
  testing: false,
  /** When the last connection test ended. */
  lastTest: Number.NEGATIVE_INFINITY,
}

export function reset(): void {
  state.view = seed()
  state.failing = false
  state.testing = false
  state.lastTest = Number.NEGATIVE_INFINITY
}

const detail = (message: string, status: number, init?: ResponseInit) =>
  HttpResponse.json({ detail: message }, { ...init, status })

export const handlers = [
  http.get(base, () => HttpResponse.json(state.view)),

  http.put(base, async ({ request }) => {
    const body = (await request.json()) as AiCredentialUpdate
    const baseUrl = body.kind === 'gateway' ? (body.base_url ?? '').replace(/\/+$/, '') : null
    if (body.kind === 'gateway' && !baseUrl) return detail('kind "gateway" needs base_url', 400)
    if (body.kind !== 'gateway' && body.base_url) return detail('base_url applies to kind "gateway" only', 400)
    if (body.secret === undefined) {
      if (!state.view.configured) return detail('no credential is stored yet; secret is required', 400)
      if (state.view.kind !== body.kind || state.view.base_url !== baseUrl) {
        return detail(
          'changing kind or base_url needs the secret again: the stored one is not sent to a new destination',
          409,
        )
      }
      return HttpResponse.json(state.view)
    }
    const secret = body.secret.trim()
    if (!secret) return detail('secret is empty', 400)
    if (/\s/.test(secret)) return detail('secret must not contain whitespace', 400)
    state.failing = secret.includes('bad')
    state.view = {
      ...EMPTY,
      configured: true,
      kind: body.kind,
      base_url: baseUrl,
      last4: secret.slice(-4),
      updated_at: new Date().toISOString(),
      usable: true,
    }
    return HttpResponse.json(state.view)
  }),

  http.delete(base, () => {
    state.view = { ...EMPTY }
    return HttpResponse.json(state.view)
  }),

  http.post(`${base}/test`, async () => {
    const wait = state.testing ? COOLDOWN_MS : state.lastTest + COOLDOWN_MS - Date.now()
    if (wait > 0) {
      return detail(
        state.testing ? 'a connection test is already running' : 'a connection test ran moments ago; try again shortly',
        429,
        { headers: { 'Retry-After': String(Math.max(1, Math.ceil(wait / 1000))) } },
      )
    }
    if (!state.view.configured) return detail('no credential is configured', 404)
    state.testing = true
    try {
      // The agent starts a Claude Code process; a moment here lets a second click meet the running test.
      await delay(20)
      return HttpResponse.json(
        state.failing
          ? { ok: false, detail: 'authentication_error: invalid x-api-key', duration_ms: 812, model: null }
          : { ok: true, detail: 'ok', duration_ms: 1430, model: 'claude-sonnet-5-5' },
      )
    } finally {
      state.testing = false
      state.lastTest = Date.now()
    }
  }),
]
