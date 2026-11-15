import { randomUUID } from 'node:crypto'
import { NOT_UP, transportKind } from './backendUp.js'
import { AGENT_CRUD, CRUD_READ_TIMEOUT_MS, CrudReadError } from './crudRead.js'

// The agent's small stores behind the API's CRUD routes (#5299 step 5; backend
// api/crud_agent_stores.py): activity kinds, browser origins, session tabs, tab owners, tab
// images, pairings and MCP tokens. Each store keeps its interface; its `Crud…` class is that
// interface over the routes, one route per method, and `gated` picks it per call once the API
// lists the store's area (api/crudAreas.ts), the SQL store until then (#5299 D9). Old API pods
// may still answer during a roll, so the SQL stays until every pod lists the areas.

/** The areas of the small stores (backend api/crud_areas.py `CRUD_AREAS["agent"]`). */
export const STORE_AREAS = [
  'activity-kinds',
  'browser-origins',
  'bridge-tabs',
  'pairings',
  'mcp-tokens',
  'notify',
  'access',
  'plugin-sessions',
  'plugins',
  'plugin-packages',
] as const
export type StoreArea = (typeof STORE_AREAS)[number]

/**
 * `crud` while `use()` holds, `sql` otherwise, chosen at each method call: an object with
 * `sql`'s shape whose every method runs on the one picked then.
 */
export function gated<T extends object>(sql: T, crud: T, use: () => boolean): T {
  return new Proxy(sql, {
    get(_target, prop) {
      const pick = use() ? crud : sql
      const value: unknown = Reflect.get(pick, prop, pick)
      return typeof value === 'function' ? (value as (...args: unknown[]) => unknown).bind(pick) : value
    },
  })
}

/** The `detail` and `reason` of a problem response. */
async function problemOf(response: Response): Promise<{ detail: string; reason?: string }> {
  const body = (await response.json().catch(() => undefined)) as { detail?: unknown; reason?: unknown } | undefined
  return {
    detail: typeof body?.detail === 'string' ? body.detail : '',
    ...(typeof body?.reason === 'string' ? { reason: body.reason } : {}),
  }
}

export type CrudCallInit = {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  /** A JSON body, or bytes sent as they are with `contentType`. */
  body?: unknown
  bytes?: Buffer
  contentType?: string
  /** Stops waiting with the call it is for; the request is dropped. */
  signal?: AbortSignal | undefined
  timeoutMs?: number
}

/**
 * One call of the agent's CRUD route `path` (under `AGENT_CRUD`): the route's own answer (only
 * one with `X-ScadBuddy-Crud: 1` counts), or CrudReadError, as `crudRead` (api/crudRead.ts)
 * answers, for a call it cannot make: a PUT or a DELETE, bytes either way, a 204, or an abort signal.
 */
export async function crudCall(fetchImpl: typeof fetch, baseUrl: string, step: string, path: string, init: CrudCallInit = {}): Promise<Response> {
  const timeout = AbortSignal.timeout(init.timeoutMs ?? CRUD_READ_TIMEOUT_MS)
  const signal = init.signal ? AbortSignal.any([init.signal, timeout]) : timeout
  init.signal?.throwIfAborted()
  const headers: Record<string, string> = {}
  let body: string | Uint8Array | undefined
  if (init.bytes !== undefined) {
    headers['content-type'] = init.contentType ?? 'application/octet-stream'
    body = new Uint8Array(init.bytes)
  } else if (init.body !== undefined) {
    headers['content-type'] = 'application/json'
    body = JSON.stringify(init.body)
  }
  let response: Response
  try {
    response = await fetchImpl(`${baseUrl.replace(/\/+$/, '')}${AGENT_CRUD}${path}`, {
      method: init.method ?? (body === undefined ? 'GET' : 'POST'),
      ...(body === undefined ? {} : { headers, body }),
      signal,
    })
  } catch (error) {
    if (init.signal?.aborted) throw init.signal.reason
    const kind = transportKind(error)
    throw new CrudReadError(`${kind === NOT_UP ? 'the backend is not up yet' : 'the backend could not be reached'} (${step}: ${kind})`)
  }
  const crud = response.headers.get('x-scadbuddy-crud') === '1'
  if (response.ok && crud) return response
  const problem = crud ? await problemOf(response) : { detail: '' }
  if (!crud) await response.body?.cancel()
  const who = crud ? 'the backend refused' : 'no CRUD route answered'
  const detail = problem.detail
  throw new CrudReadError(`${who} ${step} (HTTP ${response.status}${detail ? `: ${detail}` : ''})`, response.status, crud, problem)
}

/** `crudCall`'s JSON answer. */
export async function crudJson<T>(fetchImpl: typeof fetch, baseUrl: string, step: string, path: string, init: CrudCallInit = {}): Promise<T> {
  return (await (await crudCall(fetchImpl, baseUrl, step, path, init)).json()) as T
}

/** A store's reach of the routes: the backend's URL and the fetch that carries the service credential. */
export type CrudTarget = { baseUrl: string; fetch?: typeof fetch }

/** `step`'s JSON call on `target`. */
export function crudOf(target: CrudTarget): <T>(step: string, path: string, init?: CrudCallInit) => Promise<T> {
  const fetchImpl = target.fetch ?? globalThis.fetch
  return (step, path, init) => crudJson(fetchImpl, target.baseUrl, step, path, init)
}

/** A query string of `params`, `?` first. */
export function qs(params: Record<string, string>): string {
  return `?${new URLSearchParams(params).toString()}`
}

/** The route's reasons for a lease another holder keeps, and for one this holder lost. */
export const LEASE_BUSY = 'busy'
export const LEASE_LOST = 'lease-lost'
/** How long a lease holds without a renew, and how often its holder renews it (backend agent_data/leases.py). */
export const LEASE_TTL_S = 120
export const LEASE_RENEW_MS = 30_000

/** A lease held: its key and the holder's own token. */
export type HeldLease = { key: string; token: string }

/**
 * The agent's leases through the API's CRUD routes (#5299 D3; backend api/crud_agent_access.py,
 * `…/leases/{key}`): a lock held across work that is not the database's, as a row with an
 * expiry, since a process with no database cannot hold a session advisory lock.
 */
export class CrudLeases {
  readonly #fetch: typeof fetch
  readonly #baseUrl: string

  constructor(target: CrudTarget) {
    this.#fetch = target.fetch ?? globalThis.fetch
    this.#baseUrl = target.baseUrl
  }

  #path(key: string): string {
    return `/leases/${encodeURIComponent(key)}`
  }

  /** The lease, or `busy` when another holder keeps it (for all of `waitS` on the API). */
  async acquire(key: string, options: { ttlS?: number; waitS?: number } = {}): Promise<HeldLease | 'busy'> {
    const token = randomUUID()
    const body = { token, ttl_s: options.ttlS ?? LEASE_TTL_S, wait_s: options.waitS ?? 0 }
    try {
      await crudJson(this.#fetch, this.#baseUrl, 'a lease taken', this.#path(key), {
        body,
        timeoutMs: CRUD_READ_TIMEOUT_MS + (options.waitS ?? 0) * 1000,
      })
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.reason === LEASE_BUSY) return 'busy'
      // An answer lost on the way back may have granted it: the token is this call's own.
      await this.release({ key, token }).catch(() => undefined)
      throw err
    }
    return { key, token }
  }

  /** Whether the lease still holds. */
  async renew(lease: HeldLease, ttlS = LEASE_TTL_S): Promise<boolean> {
    try {
      await crudJson(this.#fetch, this.#baseUrl, 'a lease renewed', this.#path(lease.key), {
        method: 'PUT',
        body: { token: lease.token, ttl_s: ttlS },
      })
      return true
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.reason === LEASE_LOST) return false
      throw err
    }
  }

  async release(lease: HeldLease): Promise<void> {
    await crudCall(this.#fetch, this.#baseUrl, 'a lease released', `${this.#path(lease.key)}${qs({ token: lease.token })}`, {
      method: 'DELETE',
    })
  }

  /**
   * Runs `fn` holding `key`'s lease, renewed every `LEASE_RENEW_MS` and released at the end,
   * however `fn` ends. `busy`: another holder keeps it, and `fn` did not run. A renew or a
   * release that fails is logged: the lease then frees itself at its expiry, and a write
   * that names a lost lease is refused (`lease-lost`).
   */
  async holding<T>(key: string, fn: (lease: HeldLease) => Promise<T>, options: { waitS?: number } = {}): Promise<{ value: T } | 'busy'> {
    const lease = await this.acquire(key, options)
    if (lease === 'busy') return 'busy'
    const timer = setInterval(() => {
      void this.renew(lease).then(
        (held) => {
          if (!held) console.error(`lease ${key} was lost before its work ended`)
        },
        (err: unknown) => console.error(`lease ${key}: the renew failed: ${err instanceof Error ? err.message : String(err)}`),
      )
    }, LEASE_RENEW_MS)
    timer.unref()
    try {
      return { value: await fn(lease) }
    } finally {
      clearInterval(timer)
      await this.release(lease).catch((err: unknown) =>
        console.error(`lease ${key}: the release failed: ${err instanceof Error ? err.message : String(err)}`),
      )
    }
  }
}
