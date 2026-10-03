import { randomUUID } from 'node:crypto'
import { setTimeout as sleep } from 'node:timers/promises'
import { ok } from '../tools/call.js'
import { type ToolContext, ToolError } from '../tools/registry.js'

// A ScadBuddy command from a tool (#1052, #1053, spec 2026-10-01 §4.2), as the browser
// client's `command()` does it: one key per call, kept by every re-send, so a re-send
// after an answer that never arrived is answered with the first outcome and nothing is
// done twice. A route answers its own body, or 202 with an operation still running,
// followed here through `GET /api/v1/operations/{id}`.

export type FetchResult<T> = { data?: T; error?: unknown; response: Response }

/** How many more times a request no ScadBuddy answer described is sent (#470). */
export const RUN_REATTEMPTS = 3

/** The backend's 503 while Temporal has not yet answered a command's start (#1052). */
const STILL_ACCEPTING = 'https://scadbuddy.dev/problems/command-still-accepting'

/**
 * The request never got the backend's own answer: a 502/503/504, or Cloudflare's 524,
 * from something in between, whose body is not one of the backend's problems (they
 * always carry a `detail`). Or the backend answered that it is still accepting the same
 * request. The same rule as the browser client's `unanswered`.
 */
function unanswered(result: FetchResult<unknown>): boolean {
  const { error, response } = result
  const problem = typeof error === 'object' && error !== null ? (error as { type?: unknown; detail?: unknown }) : {}
  if (response.status === 503 && problem.type === STILL_ACCEPTING) return true
  return [502, 503, 504, 524].includes(response.status) && typeof problem.detail !== 'string'
}

/**
 * `send`'s result, sent again while it goes unanswered (a dropped connection, fetch's
 * `TypeError`, or a proxy's own 502/503/504/524). Safe only for a request keyed to its
 * effect, or one that only reads. A problem the backend wrote is never re-sent.
 */
export async function answered<T>(
  ctx: ToolContext,
  send: () => Promise<FetchResult<T>>,
  what: string,
  gaveUp = '',
): Promise<FetchResult<T>> {
  for (let tries = 0; ; tries++) {
    let result: FetchResult<T>
    try {
      result = await send()
    } catch (caught) {
      if (ctx.signal.aborted || !(caught instanceof TypeError)) throw caught
      if (tries >= RUN_REATTEMPTS) throw new ToolError(`${what}: ScadBuddy did not answer (${caught.message}).${gaveUp}`)
      await sleep(ctx.pollIntervalMs, undefined, { signal: ctx.signal })
      continue
    }
    if (unanswered(result)) {
      if (tries >= RUN_REATTEMPTS) {
        throw new ToolError(`${what}: ScadBuddy did not answer (HTTP ${result.response.status}).${gaveUp}`)
      }
      await sleep(ctx.pollIntervalMs, undefined, { signal: ctx.signal })
      continue
    }
    return result
  }
}

/** `answered`, then its body or the backend's problem as a ToolError. */
export async function reattach<T>(
  ctx: ToolContext,
  send: () => Promise<FetchResult<T>>,
  what: string,
  gaveUp = '',
): Promise<T> {
  return ok(answered(ctx, send, what, gaveUp), what)
}

type Operation = {
  id: string
  status: 'running' | 'succeeded' | 'failed'
  result?: unknown
  error?: { status: number; title: string; detail: string } | null
}

/** The `Idempotency-Key` header a command sends: 32 hex digits, one per call. */
export type CommandHeaders = { 'Idempotency-Key': string }

/**
 * Run a command route: `send` with this call's key, re-sent with the same key while it
 * goes unanswered, and a 202 followed to the operation's result. A failed operation is
 * a ToolError in the backend's own words, as the route's answer would have been.
 */
export async function command<T>(
  ctx: ToolContext,
  what: string,
  send: (headers: CommandHeaders) => Promise<FetchResult<T>>,
): Promise<T> {
  const headers = { 'Idempotency-Key': randomUUID().replaceAll('-', '') }
  const gaveUp = ' It may have been done anyway: check before trying again.'
  const first = await answered(ctx, () => send(headers), what, gaveUp)
  if (first.response.status !== 202) return ok(Promise.resolve(first), what)
  let op = first.data as unknown as Operation
  const deadline = Date.now() + ctx.renderWaitMs
  for (let step = 1; op.status === 'running'; step++) {
    if (Date.now() >= deadline) {
      throw new ToolError(`${what} is still running as operation ${op.id}: follow it with get_operation.`)
    }
    await ctx.progress(step, undefined, `${what}: running`)
    await sleep(ctx.pollIntervalMs, undefined, { signal: ctx.signal })
    const id = op.id
    op = (await reattach(
      ctx,
      () => ctx.backend.GET('/api/v1/operations/{operation_id}', { params: { path: { operation_id: id } }, signal: ctx.signal }),
      `get operation ${id}`,
    )) as Operation
  }
  if (op.status === 'failed') {
    const error = op.error
    throw new ToolError(`${what} failed (HTTP ${error?.status ?? 500})`, error?.status ?? 500, error ? `${error.title}: ${error.detail}` : undefined)
  }
  return op.result as T
}
