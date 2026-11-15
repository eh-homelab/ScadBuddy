import { NOT_UP, transportKind } from './backendUp.js'
import { AGENT_CRUD, CRUD_READ_TIMEOUT_MS, CrudReadError } from './crudRead.js'

// The agent's small stores behind the API's CRUD routes (#5299 step 5; backend
// api/crud_agent_stores.py): activity kinds, browser origins, session tabs, tab owners, tab
// images, pairings and MCP tokens. Each store keeps its interface; its `Crud…` class is that
// interface over the routes, one route per method, and `gated` picks it per call once the API
// lists the store's area (api/crudAreas.ts), the SQL store until then (#5299 D9). Old API pods
// may still answer during a roll, so the SQL stays until every pod lists the areas.

/** The areas of the small stores (backend api/crud_areas.py `CRUD_AREAS["agent"]`). */
export const STORE_AREAS = ['activity-kinds', 'browser-origins', 'bridge-tabs', 'pairings', 'mcp-tokens', 'notify'] as const
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
  method?: 'GET' | 'POST' | 'PUT'
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
 * answers, for a call it cannot make: a PUT, bytes either way, a 204, or an abort signal.
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
/** A UUID, as every id the agent's CRUD routes take in their path is. */
export const UUID_SEGMENT = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** A path segment that fails its rule: it is never sent. */
export class CrudPathError extends Error {}

/**
 * `value` as one path segment of a CRUD route, only when it passes `rule`, then encoded. A name
 * or id from a caller never reaches the URL as it is, so `../x` cannot name another route under
 * this process's service credential.
 */
export function segment(value: string, rule: RegExp, what: string): string {
  if (!rule.test(value)) throw new CrudPathError(`${what} is not valid`)
  return encodeURIComponent(value)
}

/** The most tab ids one call sends (backend api/crud_agent_stores.py `TABS_MAX`). */
export const TABS_PER_CALL = 500

/** `items` in runs of at most `size`, in order. */
export function chunks<T>(items: readonly T[], size: number): T[][] {
  const out: T[][] = []
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size))
  return out
}

export function qs(params: Record<string, string>): string {
  return `?${new URLSearchParams(params).toString()}`
}
