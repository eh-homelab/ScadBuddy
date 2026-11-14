import { NOT_UP, transportKind, WAIT_FIRST_MS, WAIT_MAX_MS } from './backendUp.js'

// The agent's CRUD routes (`/api/v1/internal/crud/agent/`, backend api/crud_agent.py), read
// with the service credential main.ts adds to `fetch` (`withServiceCredential`).
//
// The rolling deploy (#5299 D9, as #5298's workers do): a new agent may meet an API pod older
// than an area's routes. `CrudAreas` reads `GET …/crud/agent/areas` from the start, backing off,
// until the API lists every area the agent wants; until then each area's reads stay on SQL, and
// once listed they go through the routes for good. An area is listed only once all its routes
// are in (backend api/crud_areas.py `CRUD_AREAS`).

export const AGENT_CRUD = '/api/v1/internal/crud/agent'
/** The areas of the agent's reads of the backend's tables (#5299 D7). */
export const READ_AREAS = ['events', 'flows'] as const
/** How long one read of a CRUD route may take. */
export const CRUD_READ_TIMEOUT_MS = 10_000

/** A CRUD route that did not answer, or did not answer as one (its message names no URL or credential). */
export class CrudReadError extends Error {
  override name = 'CrudReadError'
  /** The answer's status, when there was one. */
  readonly status: number | undefined

  constructor(message: string, status?: number) {
    super(message)
    this.status = status
  }
}

/** The `detail` of a problem response, or ''. */
async function detailOf(response: Response): Promise<string> {
  const body = (await response.json().catch(() => undefined)) as { detail?: unknown } | undefined
  return typeof body?.detail === 'string' ? body.detail : ''
}

/**
 * The JSON answer of the agent's CRUD route `path` (under `AGENT_CRUD`), or CrudReadError. Only
 * a 2xx that carries `X-ScadBuddy-Crud: 1` is the route's: a proxy's answer, or an older API's
 * 404 for a route it lacks, is refused too.
 */
export async function crudRead<T>(
  fetchImpl: typeof fetch,
  baseUrl: string,
  step: string,
  path: string,
  init: { method?: 'GET' | 'POST'; body?: unknown; timeoutMs?: number } = {},
): Promise<T> {
  let response: Response
  try {
    response = await fetchImpl(`${baseUrl.replace(/\/+$/, '')}${AGENT_CRUD}${path}`, {
      method: init.method ?? 'GET',
      ...(init.body === undefined ? {} : { headers: { 'content-type': 'application/json' }, body: JSON.stringify(init.body) }),
      signal: AbortSignal.timeout(init.timeoutMs ?? CRUD_READ_TIMEOUT_MS),
    })
  } catch (error) {
    const kind = transportKind(error)
    throw new CrudReadError(`${kind === NOT_UP ? 'the backend is not up yet' : 'the backend could not be reached'} (${step}: ${kind})`)
  }
  const crud = response.headers.get('x-scadbuddy-crud') === '1'
  if (response.ok && crud) return (await response.json()) as T
  const detail = crud ? await detailOf(response) : ''
  if (!crud) await response.body?.cancel()
  const who = crud ? 'the backend refused' : 'no CRUD route answered'
  throw new CrudReadError(`${who} ${step} (HTTP ${response.status}${detail ? `: ${detail}` : ''})`, response.status)
}

export type CrudAreasOptions = {
  fetch?: typeof fetch
  /** The areas the agent waits for. */
  wanted: readonly string[]
  log?: (message: string) => void
  /** The back-off between reads of the areas, doubling (`WAIT_FIRST_MS` to `WAIT_MAX_MS`). */
  firstMs?: number
  maxMs?: number
}

/** Which of the agent's CRUD areas the API serves (see above). */
export class CrudAreas {
  readonly #base: string
  readonly #fetch: typeof fetch
  readonly #wanted: readonly string[]
  readonly #log: (message: string) => void
  readonly #firstMs: number
  readonly #maxMs: number
  readonly #listed = new Set<string>()
  #timer: NodeJS.Timeout | undefined
  #started = false
  #stopped = false
  #waitLogged = false

  constructor(baseUrl: string, options: CrudAreasOptions) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
    this.#wanted = options.wanted
    this.#log = options.log ?? ((m) => console.log(m))
    this.#firstMs = options.firstMs ?? WAIT_FIRST_MS
    this.#maxMs = options.maxMs ?? WAIT_MAX_MS
  }

  /** Whether the API serves `area`: once true, true for good. */
  has(area: string): boolean {
    return this.#listed.has(area)
  }

  /** Whether every wanted area is served. */
  get complete(): boolean {
    return this.#wanted.every((a) => this.#listed.has(a))
  }

  /** Reads the areas until every wanted one is listed, in the background; never throws. */
  start(): void {
    if (this.#started) return
    this.#started = true
    void this.#poll(this.#firstMs)
  }

  stop(): void {
    this.#stopped = true
    clearTimeout(this.#timer)
  }

  /** One read of the areas; whether every wanted one is listed now. Logs once while it waits. */
  async refresh(): Promise<boolean> {
    let why: string
    try {
      const { areas } = await crudRead<{ areas: unknown }>(this.#fetch, this.#base, 'its areas', '/areas')
      const before = this.complete
      for (const area of Array.isArray(areas) ? areas : []) {
        if (typeof area === 'string' && this.#wanted.includes(area)) this.#listed.add(area)
      }
      if (this.complete) {
        if (!before) this.#log(`CRUD areas: the agent reads ${this.#wanted.join(', ')} through the backend's CRUD routes`)
        return true
      }
      why = `missing ${this.#wanted.filter((a) => !this.#listed.has(a)).join(', ')}`
    } catch (error) {
      why = (error as Error).message
    }
    if (!this.#waitLogged) {
      this.#waitLogged = true
      this.#log(`CRUD areas: waiting for the backend's agent areas (${why}); reading over SQL until it lists them`)
    }
    return false
  }

  async #poll(delay: number): Promise<void> {
    if (this.#stopped || (await this.refresh()) || this.#stopped) return
    this.#timer = setTimeout(() => void this.#poll(Math.min(delay * 2, this.#maxMs)), delay)
    this.#timer.unref()
  }
}
