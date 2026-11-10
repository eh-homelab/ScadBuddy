// #5868: the API pod's agent sidecar can start before the backend container answers on
// 127.0.0.1:8080. Node's fetch to a port nothing listens on rejects with a TypeError
// ('fetch failed') whose cause carries the code ECONNREFUSED (an AggregateError of such
// errors when it tried both address families). That is the backend not up yet, not a failure.

/** How long the start-up wait sleeps after its first refused try, doubling to `WAIT_MAX_MS`. */
export const WAIT_FIRST_MS = 1_000
export const WAIT_MAX_MS = 30_000

/** The kind of transport error that is the backend not up yet: nothing listens at its address. */
export const NOT_UP = 'ECONNREFUSED'

function codeOf(error: unknown): string | undefined {
  const code = typeof error === 'object' && error !== null ? (error as { code?: unknown }).code : undefined
  return typeof code === 'string' ? code : undefined
}

/**
 * The kind of a fetch's transport error, for a log line: its cause's code (`ECONNREFUSED`,
 * `ENOTFOUND`, ...; for an AggregateError, `NOT_UP` when any of its errors is a refusal, else
 * its first code), else the error's name (`TimeoutError`).
 */
export function transportKind(error: unknown): string {
  const cause = error instanceof Error ? error.cause : undefined
  // `localhost` tried on ::1, then 127.0.0.1: a refusal by either is the backend not up yet,
  // whatever came first (Node may set the AggregateError's own code to the first one).
  const codes = cause instanceof AggregateError ? cause.errors.map(codeOf) : []
  if (codes.includes(NOT_UP)) return NOT_UP
  const code = codeOf(cause) ?? codes.find((c) => c !== undefined)
  if (code !== undefined) return code
  return error instanceof Error ? error.name : 'Error'
}

/** Whether a fetch rejected because nothing listens at the backend's address: the backend is not up yet. */
export function backendNotUp(error: unknown): boolean {
  return error instanceof TypeError && transportKind(error) === NOT_UP
}

/**
 * `call`'s answer, once the backend answers. While `kind` names the transport error of a
 * backend that does not answer, it tries again after `WAIT_FIRST_MS`, doubling to
 * `WAIT_MAX_MS`, and never gives up. The backend not up yet (`NOT_UP`) is logged once with
 * `log`, as waiting; any other kind (a host that does not resolve, a timeout: a URL that may
 * never answer) is logged with `error`, once per kind, so a bad URL is seen (#5889). An error
 * `kind` gives no kind for is thrown. The messages are the errors', which must name no URL or
 * credential. The timer does not hold the process open.
 */
export async function untilBackendAnswers<T>(
  call: () => Promise<T>,
  options: {
    kind: (error: unknown) => string | undefined
    log: (message: string) => void
    error: (message: string) => void
    sleep?: (ms: number) => Promise<void>
  },
): Promise<T> {
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms).unref()))
  let delay = WAIT_FIRST_MS
  const logged = new Set<string>()
  for (;;) {
    try {
      return await call()
    } catch (error) {
      const kind = options.kind(error)
      if (kind === undefined) throw error
      if (!logged.has(kind)) {
        const message = (error as Error).message
        if (kind === NOT_UP) options.log(`waiting for the backend: ${message}`)
        else options.error(`${message}: ${kind}; trying again until it answers`)
      }
      logged.add(kind)
      await sleep(delay)
      delay = Math.min(delay * 2, WAIT_MAX_MS)
    }
  }
}
