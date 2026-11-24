import { NOT_UP, transportKind } from './backendUp.js'
import { refuseDotSegments } from './pathSegments.js'

// The agent's CRUD routes (`/api/v1/internal/crud/agent/`, backend api/crud_agent.py), read
// with the service credential main.ts adds to `fetch` (`withServiceCredential`). The agent's
// reads of the backend's tables (#5299 D7, areas `events` and `flows`) go only through them,
// with no SQL fallback: the agent has no SQL client (#5299 step 12), and
// test/retiredTables.test.ts fails on a Postgres driver in `src`.
// Its settings, credentials and payload keys (#5299 step 4), and its forget, key deletes and flow
// images (#5299 step 6), wait for their areas
// (api/crudAreas.ts).

export const AGENT_CRUD = '/api/v1/internal/crud/agent'
/** How long one read of a CRUD route may take. */
export const CRUD_READ_TIMEOUT_MS = 10_000

/**
 * The transport errors of a connection to the API that dropped or was refused, as one does while
 * a rollout ends the API's old pod (#6994: a turn's transcript read failed `UND_ERR_SOCKET` on
 * 2026-11-22, and the person saw it). Not a timeout: the route may have been slow, not gone.
 */
export const TRANSIENT_KINDS: ReadonlySet<string> = new Set(['UND_ERR_SOCKET', 'UND_ERR_CLOSED', 'ECONNRESET', 'ECONNREFUSED', 'EPIPE'])
/**
 * The waits before each retry of an idempotent call after such an error: bounded, about 4 s in all,
 * then CrudReadError. Not after a refusal (`NOT_UP`): nothing listens yet, and the callers that
 * wait for the backend to start keep their own back-off and stop (`backendUp.ts`, #5868).
 */
export const RETRY_DELAYS_MS: readonly number[] = [250, 1_000, 3_000]

/** One field a 422 refused: its `loc` joined with `.`, and the API's message (`''` without one). */
export type CrudFieldError = { field: string; msg: string }

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
  /** A 422's refused fields, each `loc` joined with `.` (`body.turn_id`); none on another problem. */
  readonly fields: readonly string[]
  /** A 422's refused fields with the API's message for each (`errors[].msg`), in `fields`' order. */
  readonly errors: readonly CrudFieldError[]
  /** The transport error's kind (`backendUp.ts` `transportKind`) when no answer came. */
  readonly transport: string | undefined

  constructor(
    message: string,
    status?: number,
    fromRoute = false,
    problem: { detail?: string; reason?: string; fields?: readonly string[]; errors?: readonly CrudFieldError[] } = {},
    transport?: string,
  ) {
    super(message)
    this.status = status
    this.fromRoute = fromRoute
    this.detail = problem.detail ?? ''
    this.reason = problem.reason
    this.errors = problem.errors ?? (problem.fields ?? []).map((field) => ({ field, msg: '' }))
    this.fields = problem.fields ?? this.errors.map((e) => e.field)
    this.transport = transport
  }

  /** Whether the connection dropped or was refused (`TRANSIENT_KINDS`): the API may answer again in a moment. */
  get transient(): boolean {
    return this.transport !== undefined && TRANSIENT_KINDS.has(this.transport)
  }
}

/** Whether `error`, or an error it was caused by, is a CRUD call whose connection dropped or was refused (#6994). */
export function backendDropped(error: unknown): boolean {
  for (let e = error, depth = 0; e instanceof Error && depth < 8; e = e.cause, depth++) {
    if (e instanceof CrudReadError) return e.transient
  }
  return false
}

/** Waits `ms`, or rejects with `signal`'s reason once it aborts. */
function pause(ms: number, signal: AbortSignal | undefined): Promise<void> {
  return new Promise((resolve, reject) => {
    // Already aborted (the deadline fired in the try): its abort event has gone, so it never comes.
    if (signal?.aborted) return reject(signal.reason)
    const done = () => {
      clearTimeout(timer)
      reject(signal!.reason)
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', done)
      resolve()
    }, ms)
    signal?.addEventListener('abort', done, { once: true })
  })
}

/**
 * `fetchImpl`'s answer to `url`, or CrudReadError when none came within `timeoutMs`, one deadline
 * for the call and all its tries (as one try had before #6994). An idempotent call (`retry`) whose
 * connection dropped (`TRANSIENT_KINDS`, but `NOT_UP`) is tried again after each of
 * RETRY_DELAYS_MS while the deadline allows; any other call is never sent twice, since a write
 * whose answer was lost may have landed. `signal` is the caller's: once it aborts, its reason is thrown.
 * A URL whose path names a `.` or `..` segment is never sent: DotSegmentError (`pathSegments.ts`).
 */
export async function crudFetch(
  fetchImpl: typeof fetch,
  url: string,
  step: string,
  init: Omit<RequestInit, 'signal'>,
  options: { retry: boolean; timeoutMs?: number; signal?: AbortSignal | undefined },
): Promise<Response> {
  options.signal?.throwIfAborted()
  // A `.` or `..` segment would reach another route once the URL normalises it (#7221).
  refuseDotSegments(url, step)
  const deadline = AbortSignal.timeout(options.timeoutMs ?? CRUD_READ_TIMEOUT_MS)
  const signal = options.signal ? AbortSignal.any([options.signal, deadline]) : deadline
  const failed = (kind: string) =>
    new CrudReadError(`${kind === NOT_UP ? 'the backend is not up yet' : 'the backend could not be reached'} (${step}: ${kind})`, undefined, false, {}, kind)
  for (let tries = 0; ; tries++) {
    try {
      return await fetchImpl(url, { ...init, signal })
    } catch (error) {
      if (options.signal?.aborted) throw options.signal.reason
      const kind = transportKind(error)
      const wait = RETRY_DELAYS_MS[tries]
      if (!(options.retry && kind !== NOT_UP && TRANSIENT_KINDS.has(kind) && wait !== undefined)) throw failed(kind)
      try {
        await pause(wait, signal)
      } catch {
        if (options.signal?.aborted) throw options.signal.reason
        // The deadline ended the wait: the call fails as its last try did.
        throw failed(kind)
      }
    }
  }
}

/**
 * `value` with every string well-formed, an object's keys included, for a CRUD route's body. A string cut between the
 * halves of a pair (`slice(0, 500)`) holds a lone surrogate, which JSON escapes and the API
 * refuses as no Unicode (422); Postgres.js wrote one as U+FFFD over SQL, and so does this.
 */
export function wellFormed(value: unknown): unknown {
  if (typeof value === 'string') return value.toWellFormed()
  if (Array.isArray(value)) return value.map(wellFormed)
  if (value && typeof value === 'object' && !(value instanceof Date)) {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([k, v]) => [k.toWellFormed(), wellFormed(v)]))
  }
  return value
}

/** The `detail`, `reason` and, on a 422, refused fields (`errors[].loc` and `.msg`, backend crud/guard.py) of a problem response. */
async function problemOf(response: Response): Promise<{ detail: string; reason?: string; errors: CrudFieldError[] }> {
  const body = (await response.json().catch(() => undefined)) as { detail?: unknown; reason?: unknown; errors?: unknown } | undefined
  const raw = response.status === 422 && Array.isArray(body?.errors) ? (body.errors as unknown[]) : []
  const errors = raw.flatMap((e) => {
    const { loc, msg } = (e ?? {}) as { loc?: unknown; msg?: unknown }
    return Array.isArray(loc) ? [{ field: loc.map(String).join('.'), msg: typeof msg === 'string' ? msg : '' }] : []
  })
  return {
    detail: typeof body?.detail === 'string' ? body.detail : '',
    ...(typeof body?.reason === 'string' ? { reason: body.reason } : {}),
    errors,
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
  init: { method?: 'GET' | 'POST'; body?: unknown; timeoutMs?: number; idempotent?: boolean } = {},
): Promise<T> {
  const response = await crudResponse(fetchImpl, baseUrl, step, path, {
    ...(init.method === undefined ? {} : { method: init.method }),
    ...(init.idempotent === undefined ? {} : { idempotent: init.idempotent }),
    ...(init.timeoutMs === undefined ? {} : { timeoutMs: init.timeoutMs }),
    ...(init.body === undefined ? {} : { body: JSON.stringify(init.body), contentType: 'application/json' }),
  })
  return (await response.json()) as T
}

/**
 * The route's own 2xx answer to `path`, unread, or CrudReadError, as `crudRead` judges it: for a
 * body that is not JSON (a flow run's images as multipart, #5299 D8), or an answer that is bytes.
 * Without `contentType`, a `FormData` body sets its own. A GET, or a POST marked `idempotent` (a
 * read the route takes a body for), is tried again when its connection drops (`crudFetch`).
 */
export async function crudResponse(
  fetchImpl: typeof fetch,
  baseUrl: string,
  step: string,
  path: string,
  init: { method?: 'GET' | 'POST'; body?: RequestInit['body']; contentType?: string; timeoutMs?: number; idempotent?: boolean } = {},
): Promise<Response> {
  const method = init.method ?? 'GET'
  const response = await crudFetch(
    fetchImpl,
    `${baseUrl.replace(/\/+$/, '')}${AGENT_CRUD}${path}`,
    step,
    {
      method,
      ...(init.body === undefined ? {} : { body: init.body }),
      ...(init.contentType === undefined ? {} : { headers: { 'content-type': init.contentType } }),
    },
    { retry: method === 'GET' || init.idempotent === true, ...(init.timeoutMs === undefined ? {} : { timeoutMs: init.timeoutMs }) },
  )
  const crud = response.headers.get('x-scadbuddy-crud') === '1'
  if (response.ok && crud) return response
  const problem = crud ? await problemOf(response) : { detail: '' }
  if (!crud) await response.body?.cancel()
  const who = crud ? 'the backend refused' : 'no CRUD route answered'
  const detail = problem.detail
  throw new CrudReadError(`${who} ${step} (HTTP ${response.status}${detail ? `: ${detail}` : ''})`, response.status, crud, problem)
}
