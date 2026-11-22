import { readFile } from 'node:fs/promises'
import type { Middleware } from 'openapi-fetch'
import { backendNotUp } from './backendUp.js'

// #4975 phase 4 (spec 2026-11-01 §7.1, §7.2): the agent's service credential on every call
// to the backend: the openapi-fetch client (`serviceCredentialMiddleware`), the CRUD routes and
// the workspace blob routes' plain fetches (`withServiceCredential`). It is exchanged for the
// proof the pod mounts (`ExchangeCredential`, below); the agent has no database view to read a
// sealed one from (#5299 step 12), so a proof is required (`config.ts` `serviceProof`).

/** Where the agent's credential comes from: the exchange (`ExchangeCredential`); tests give a fixed one. */
export type CredentialSource = {
  current(): Promise<string | undefined>
  /** The backend answered 401 to `token`. */
  invalidate(token: string): void
}

// #5494 (spec 2026-11-05 §6.2, §6.3, §8 step 1): with SCADBUDDY_SERVICE_TOKEN_FILE (the projected
// ServiceAccount token, audience `scadbuddy`) or SCADBUDDY_SERVICE_BOOTSTRAP_FILE, the agent exchanges
// its proof at the backend's POST /api/v1/auth/service-token for a 15-minute credential with no
// sealed copy. Mirrors `backend/scadbuddy/auth/service_credentials.py`
// `ExchangeReader`.

export const EXCHANGE_PATH = '/api/v1/auth/service-token'
const GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:token-exchange'
const TOKEN_TYPES = {
  k8s: 'urn:ietf:params:oauth:token-type:jwt',
  bootstrap: 'urn:scadbuddy:params:token-type:bootstrap',
} as const
/**
 * A failed exchange is tried again after 5 s, doubling to 60 s (§6.3). A refused connection
 * (the backend not up yet, #5868) is not a failure: it is tried again at the next call, which
 * goes to the same backend and would be refused too, and logged once until the backend answers.
 */
export const EXCHANGE_RETRY_FIRST_MS = 5_000
export const EXCHANGE_RETRY_MAX_MS = 60_000
/** A 401 exchanges again at once, but never sooner than this after the last exchange. */
export const REEXCHANGE_MIN_MS = 10_000
const EXCHANGE_TIMEOUT_MS = 10_000
const TOKEN_RE = /^sbsvc_[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$/

export type ExchangeOptions = {
  backendUrl: string
  proofFile: string
  proof: keyof typeof TOKEN_TYPES
  fetchImpl?: typeof fetch
  /** Reads the proof file again before each exchange: the kubelet rotates a projected token. */
  readProof?: (path: string) => Promise<string>
  now?: () => number
  log?: (message: string) => void
}

export class ExchangeCredential implements CredentialSource {
  private token: string | undefined
  private expiresAt = 0
  private renewAt = 0
  private nextTry = 0
  private delay = EXCHANGE_RETRY_FIRST_MS
  private exchangedAt: number | undefined
  /** Whether "waiting for the backend" was logged since the backend last answered. */
  private waiting = false
  private exchanging: Promise<void> | undefined

  private readonly url: string
  private readonly options: ExchangeOptions
  private readonly fetchImpl: typeof fetch
  private readonly readProof: (path: string) => Promise<string>
  private readonly now: () => number
  private readonly log: (message: string) => void

  constructor(options: ExchangeOptions) {
    this.options = options
    this.url = options.backendUrl.replace(/\/+$/, '') + EXCHANGE_PATH
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch
    this.readProof = options.readProof ?? ((path) => readFile(path, 'utf8'))
    this.now = options.now ?? Date.now
    this.log = options.log ?? ((message) => console.error(message))
  }

  async current(): Promise<string | undefined> {
    if (this.token !== undefined && this.now() < this.renewAt) return this.token
    if (this.now() >= this.nextTry) {
      this.exchanging ??= this.exchange().finally(() => {
        this.exchanging = undefined
      })
      await this.exchanging
    }
    if (this.token !== undefined && this.now() < this.expiresAt) return this.token
    return undefined
  }

  invalidate(token: string): void {
    if (token !== this.token) return
    this.token = undefined
    this.nextTry = Math.max(this.now(), (this.exchangedAt ?? 0) + REEXCHANGE_MIN_MS)
  }

  private failed(reason: string): void {
    this.log(`could not exchange the agent's service credential (${reason})`)
    this.nextTry = this.now() + this.delay
    this.delay = Math.min(this.delay * 2, EXCHANGE_RETRY_MAX_MS)
  }

  private async exchange(): Promise<void> {
    let proof: string
    try {
      proof = (await this.readProof(this.options.proofFile)).trim()
    } catch (error) {
      this.failed(`no proof: ${(error as Error).name}`)
      return
    }
    if (!proof) {
      this.failed('empty proof')
      return
    }
    let response: Response
    try {
      response = await this.fetchImpl(this.url, {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: GRANT_TYPE,
          subject_token: proof,
          subject_token_type: TOKEN_TYPES[this.options.proof],
          // Never believed: when sent, the backend refuses a proof of another caller.
          client_id: 'agent',
        }).toString(),
        signal: AbortSignal.timeout(EXCHANGE_TIMEOUT_MS),
      })
    } catch (error) {
      if (backendNotUp(error)) {
        if (!this.waiting) this.log("the agent's service credential: waiting for the backend, which is not up yet")
        this.waiting = true
        return
      }
      this.failed((error as Error).name)
      return
    }
    this.waiting = false
    const now = this.now()
    if (response.status === 200) {
      let body: { access_token?: unknown; expires_in?: unknown }
      try {
        body = (await response.json()) as typeof body
      } catch {
        this.failed('an answer without a token')
        return
      }
      const token = body.access_token
      const lifetime = Number(body.expires_in) * 1000
      if (typeof token !== 'string' || !TOKEN_RE.test(token) || !(lifetime > 0)) {
        this.failed('an answer without a token')
        return
      }
      this.token = token
      this.exchangedAt = now
      this.expiresAt = now + lifetime
      this.renewAt = now + lifetime / 2
      this.nextTry = this.renewAt
      this.delay = EXCHANGE_RETRY_FIRST_MS
      return
    }
    await response.body?.cancel()
    if (response.status === 429) {
      const wait = Number(response.headers.get('retry-after'))
      this.log("the agent's service-token exchange was rate limited")
      this.nextTry = now + (Number.isFinite(wait) && wait >= 1 ? wait * 1000 : this.delay)
      return
    }
    this.failed(`HTTP ${response.status}`)
  }
}

/**
 * `fetchImpl` with the bearer added to requests under `baseUrl` (the backend) only, so a
 * presigned transfer URL elsewhere never sees it. A 401 drops that token, so the next
 * request carries a fresh one; the refused request is not sent again (a write might run twice).
 */
export function withServiceCredential(fetchImpl: typeof fetch, credential: CredentialSource, baseUrl: string): typeof fetch {
  const base = baseUrl.replace(/\/+$/, '') + '/'
  return async (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (!url.startsWith(base)) return fetchImpl(input, init)
    const token = await credential.current()
    if (token === undefined) return fetchImpl(input, init)
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined))
    if (!headers.has('authorization')) headers.set('authorization', `Bearer ${token}`)
    const response = await fetchImpl(input, { ...init, headers })
    if (response.status === 401 && headers.get('authorization') === `Bearer ${token}`) credential.invalidate(token)
    return response
  }
}

/** `Authorization: Bearer <token>` on every backend call, when there is one; a 401 drops it. */
export function serviceCredentialMiddleware(credential: CredentialSource): Middleware {
  return {
    async onRequest({ request }) {
      const token = await credential.current()
      if (token !== undefined && !request.headers.has('authorization')) {
        request.headers.set('authorization', `Bearer ${token}`)
      }
      return request
    },
    onResponse({ request, response }) {
      const sent = request.headers.get('authorization')
      if (response.status === 401 && sent?.startsWith('Bearer sbsvc_')) credential.invalidate(sent.slice('Bearer '.length))
      return undefined
    },
  }
}
