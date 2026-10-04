import { HttpResponse, delay, http } from 'msw'
import type {
  AiCredentialCreate,
  AiCredentialEntry,
  AiCredentialList,
  AiCredentialSave,
} from '../../api/aiCredential'

/**
 * The agent service's Claude credentials (#255, #1000, #1093; `agent/src/routes/credentials.ts`)
 * for vitest and the mocked e2e run: the list routes under `/api/v1/ai/credentials/entries` and
 * `/order`, with the same shapes and save rules (`planPut`: a kind or base URL change needs the
 * secret again), and the connection test's limits as the route has them: one test at a time, and
 * 10 s from the end of one to the start of the next, refused with 429 and a `Retry-After` of at
 * least 1 s. Like the service, it keeps only the last four characters of a secret, and none of a
 * short one. A secret containing `bad` fails its connection test.
 *
 * The seed has a working key first and a rate-limited gateway second, so the list shows both
 * states; `setCredentials` lets a test start from any list.
 */

const base = '/api/v1/ai/credentials'
const COOLDOWN_MS = 10_000

function entry(over: Partial<AiCredentialEntry> & Pick<AiCredentialEntry, 'id' | 'priority'>): AiCredentialEntry {
  return {
    kind: 'anthropic_api_key',
    base_url: null,
    last4: '',
    updated_at: '2026-09-28T14:02:00Z',
    usable: true,
    status: 'active',
    cooldown_until: null,
    last_error: null,
    last_error_at: null,
    last_used_at: null,
    ...over,
  }
}

function seed(): AiCredentialEntry[] {
  return [
    entry({ id: 'default', priority: 0, last4: 'Q7xA', last_used_at: '2026-10-03T17:40:00Z' }),
    entry({
      id: 'c2',
      priority: 1,
      kind: 'gateway',
      base_url: 'https://gateway.example/anthropic',
      last4: 'GW99',
      status: 'cooling_down',
      cooldown_until: '2099-01-01T12:30:00Z',
      last_error: 'rate_limit_error: 429',
      last_error_at: '2026-10-03T17:41:00Z',
    }),
  ]
}

const state = {
  credentials: seed(),
  canSave: true,
  /** Ids whose secret fails the connection test. */
  failing: new Set<string>(),
  testing: false,
  /** When the last connection test ended. */
  lastTest: Number.NEGATIVE_INFINITY,
  nextId: 3,
}

export function reset(): void {
  state.credentials = seed()
  state.canSave = true
  state.failing = new Set()
  state.testing = false
  state.lastTest = Number.NEGATIVE_INFINITY
  state.nextId = 3
}

/** Tests: start from this list (renumbered by position) and save rule. */
export function setCredentials(credentials: AiCredentialEntry[], canSave = true): void {
  state.credentials = credentials.map((c, i) => ({ ...c, priority: i }))
  state.canSave = canSave
}

/** Tests: one credential with the given fields, the rest as a fresh active key. */
export function credentialEntry(over: Partial<AiCredentialEntry> & Pick<AiCredentialEntry, 'id'>): AiCredentialEntry {
  return entry({ priority: 0, ...over })
}

/** As the agent reads it: a cooldown that has ended is `active` again, with no write. */
function expire(): void {
  const now = Date.now()
  state.credentials = state.credentials.map((c) =>
    c.status === 'cooling_down' && c.cooldown_until && Date.parse(c.cooldown_until) <= now
      ? { ...c, status: 'active', cooldown_until: null }
      : c,
  )
}

function listView(): AiCredentialList {
  expire()
  const usable = state.credentials.filter((c) => c.usable && c.status === 'active')
  // Agent `soonestRecovery`: only a credential the mounted key opens recovers.
  const cooling = state.credentials
    .filter((c) => c.usable && c.status === 'cooling_down' && c.cooldown_until)
    .map((c) => c.cooldown_until!)
    .sort()
  return {
    credentials: state.credentials,
    usable_now: usable.length > 0,
    recovers_at: usable.length > 0 ? null : (cooling[0] ?? null),
    can_save: state.canSave,
    cannot_save_reason: state.canSave ? null : 'no key-encryption key: SCADBUDDY_SECRET_KEY_FILE is not set',
  }
}

const detail = (message: string, status: number, init?: ResponseInit) =>
  HttpResponse.json({ detail: message }, { ...init, status })

/** The body's kind, base URL and secret checked as `planPut` does, or the refusal. */
function checked(body: AiCredentialSave): { baseUrl: string | null; secret?: string } | Response {
  const baseUrl = body.kind === 'gateway' ? (body.base_url ?? '').replace(/\/+$/, '') : null
  if (body.kind === 'gateway' && !baseUrl) return detail('kind "gateway" needs base_url', 400)
  if (body.kind !== 'gateway' && body.base_url) return detail('base_url applies to kind "gateway" only', 400)
  if (body.secret === undefined) return { baseUrl }
  const secret = body.secret.trim()
  if (!secret) return detail('secret is empty', 400)
  if (/\s/.test(secret)) return detail('secret must not contain whitespace', 400)
  if (!state.canSave) return detail('credentials cannot be saved: no key-encryption key is configured', 503)
  return { baseUrl, secret }
}

/** agent secrets.ts `last4()`: nothing for a secret shorter than 12 characters. */
const last4 = (secret: string) => (secret.length >= 12 ? secret.slice(-4) : '')

function find(id: string | readonly string[] | undefined): AiCredentialEntry | undefined {
  return state.credentials.find((c) => c.id === id)
}

export const handlers = [
  http.get(`${base}/entries`, () => HttpResponse.json(listView())),

  http.post(`${base}/entries`, async ({ request }) => {
    const body = (await request.json()) as AiCredentialCreate
    const ok = checked(body)
    if (ok instanceof Response) return ok
    if (ok.secret === undefined) return detail('secret: Required', 400)
    // Agent MAX_CREDENTIALS and TOO_MANY_MESSAGE.
    if (state.credentials.length >= 100) {
      return detail('at most 100 Claude credentials can be stored; delete one first', 409)
    }
    const created = entry({
      id: `c${state.nextId++}`,
      priority: state.credentials.length,
      kind: body.kind,
      base_url: ok.baseUrl,
      last4: last4(ok.secret),
      updated_at: new Date().toISOString(),
    })
    if (ok.secret.includes('bad')) state.failing.add(created.id)
    state.credentials = [...state.credentials, created]
    return HttpResponse.json(created, { status: 201 })
  }),

  http.put(`${base}/order`, async ({ request }) => {
    const { ids } = (await request.json()) as { ids: string[] }
    const known = state.credentials.map((c) => c.id)
    if (ids.length !== known.length || new Set(ids).size !== ids.length || !ids.every((id) => known.includes(id))) {
      return detail('ids must name every credential exactly once; the list changed, read it again', 409)
    }
    state.credentials = ids.map((id, priority) => ({ ...find(id)!, priority }))
    return HttpResponse.json(listView())
  }),

  http.put(`${base}/entries/:id`, async ({ params, request }) => {
    const current = find(params.id)
    if (!current) return detail('no such credential', 404)
    const body = (await request.json()) as AiCredentialSave
    const ok = checked(body)
    if (ok instanceof Response) return ok
    if (ok.secret === undefined) {
      if (current.kind !== body.kind || current.base_url !== ok.baseUrl) {
        return detail(
          'changing kind or base_url needs the secret again: the stored one is not sent to a new destination',
          409,
        )
      }
      return HttpResponse.json(current)
    }
    const saved: AiCredentialEntry = {
      ...current,
      kind: body.kind,
      base_url: ok.baseUrl,
      last4: last4(ok.secret),
      updated_at: new Date().toISOString(),
      usable: true,
      status: 'active',
      cooldown_until: null,
      last_error: null,
      last_error_at: null,
    }
    if (ok.secret.includes('bad')) state.failing.add(saved.id)
    else state.failing.delete(saved.id)
    state.credentials = state.credentials.map((c) => (c.id === saved.id ? saved : c))
    return HttpResponse.json(saved)
  }),

  http.delete(`${base}/entries/:id`, ({ params }) => {
    if (!find(params.id)) return detail('no such credential', 404)
    state.credentials = state.credentials.filter((c) => c.id !== params.id).map((c, priority) => ({ ...c, priority }))
    return HttpResponse.json(listView())
  }),

  http.post(`${base}/entries/:id/reset`, ({ params }) => {
    const current = find(params.id)
    if (!current) return detail('no such credential', 404)
    const reset: AiCredentialEntry = {
      ...current,
      status: 'active',
      cooldown_until: null,
      last_error: null,
      last_error_at: null,
    }
    state.credentials = state.credentials.map((c) => (c.id === reset.id ? reset : c))
    return HttpResponse.json(reset)
  }),

  http.post(`${base}/entries/:id/test`, async ({ params }) => {
    const wait = state.testing ? COOLDOWN_MS : state.lastTest + COOLDOWN_MS - Date.now()
    if (wait > 0) {
      return detail(
        state.testing ? 'a connection test is already running' : 'a connection test ran moments ago; try again shortly',
        429,
        { headers: { 'Retry-After': String(Math.max(1, Math.ceil(wait / 1000))) } },
      )
    }
    const current = find(params.id)
    if (!current) return detail('no such credential', 404)
    if (!current.usable) {
      return detail('the stored credential cannot be decrypted: no key opens it; save it again', 409)
    }
    state.testing = true
    try {
      // The agent starts a Claude Code process; a moment here lets a second click meet the running test.
      await delay(20)
      const result = state.failing.has(current.id)
        ? { ok: false, detail: 'authentication_error: invalid x-api-key', duration_ms: 812, model: null }
        : { ok: true, detail: 'ok', duration_ms: 1430, model: 'claude-sonnet-5-5' }
      // As the agent's `lastTestEnded`: only a test that ran starts the cooldown.
      state.lastTest = Date.now()
      return HttpResponse.json(result)
    } finally {
      state.testing = false
    }
  }),
]
