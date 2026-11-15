import { NOT_UP, transportKind } from './backendUp.js'

// The agent's CRUD routes (`/api/v1/internal/crud/agent/`, backend api/crud_agent.py), read
// with the service credential main.ts adds to `fetch` (`withServiceCredential`). The agent's
// reads of the backend's tables (#5299 D7, areas `events` and `flows`) go only through them,
// with no SQL fallback. The API takes back its role's grants on those tables at every start
// (backend auth/revoke.py `RETIRED_GRANTS`), but the agent still connects as the database
// owner, so nothing in Postgres stops such SQL until #5299 step 12 removes its database URL;
// until then test/retiredTables.test.ts fails on agent SQL that names one.
// Its settings, credentials and payload keys (#5299 step 4) wait for their areas
// (api/crudAreas.ts).

export const AGENT_CRUD = '/api/v1/internal/crud/agent'
/** How long one read of a CRUD route may take. */
export const CRUD_READ_TIMEOUT_MS = 10_000

/** A CRUD route that did not answer, or did not answer as one (its message names no URL or credential). */
export class CrudReadError extends Error {
  override name = 'CrudReadError'
  /** The answer's status, when there was one. */
  readonly status: number | undefined
  /** Whether the answer was the route's own (`X-ScadBuddy-Crud: 1`), not a proxy's. */
  readonly fromRoute: boolean
  /** The route's own problem: its `detail` ('' without one) and `reason`, when it gave one. */
  readonly detail: string
  readonly reason: string | undefined

  constructor(message: string, status?: number, fromRoute = false, problem: { detail?: string; reason?: string } = {}) {
    super(message)
    this.status = status
    this.fromRoute = fromRoute
    this.detail = problem.detail ?? ''
    this.reason = problem.reason
  }
}

/** The `detail` and `reason` of a problem response. */
async function problemOf(response: Response): Promise<{ detail: string; reason?: string }> {
  const body = (await response.json().catch(() => undefined)) as { detail?: unknown; reason?: unknown } | undefined
  return {
    detail: typeof body?.detail === 'string' ? body.detail : '',
    ...(typeof body?.reason === 'string' ? { reason: body.reason } : {}),
  }
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
  const problem = crud ? await problemOf(response) : { detail: '' }
  if (!crud) await response.body?.cancel()
  const who = crud ? 'the backend refused' : 'no CRUD route answered'
  const detail = problem.detail
  throw new CrudReadError(`${who} ${step} (HTTP ${response.status}${detail ? `: ${detail}` : ''})`, response.status, crud, problem)
}
