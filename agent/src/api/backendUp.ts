// #5868: the API pod's agent sidecar can start before the backend container answers on
// 127.0.0.1:8080. Node's fetch to a port nothing listens on rejects with a TypeError
// ('fetch failed') whose cause carries the code ECONNREFUSED (an AggregateError of such
// errors when it tried both address families). That is the backend not up yet, not a failure.

/** How long the start-up wait sleeps after its first refused try, doubling to `WAIT_MAX_MS`. */
export const WAIT_FIRST_MS = 1_000
export const WAIT_MAX_MS = 30_000

function refused(error: unknown): boolean {
  return typeof error === 'object' && error !== null && (error as { code?: unknown }).code === 'ECONNREFUSED'
}

/** Whether a fetch rejected because nothing listens at the backend's address: the backend is not up yet. */
export function backendNotUp(error: unknown): boolean {
  if (!(error instanceof TypeError)) return false
  const cause = (error as { cause?: unknown }).cause
  return refused(cause) || (cause instanceof AggregateError && cause.errors.some(refused))
}

/**
 * `call`'s answer, once the backend answers: while `unreachable` says its error is the
 * backend not answering, it logs once that it waits (the error's message, which must name no
 * URL or credential) and tries again after `WAIT_FIRST_MS`, doubling to `WAIT_MAX_MS`. Any
 * other error is thrown. It never gives up; the timer does not hold the process open.
 */
export async function untilBackendAnswers<T>(
  call: () => Promise<T>,
  options: {
    unreachable: (error: unknown) => boolean
    log: (message: string) => void
    sleep?: (ms: number) => Promise<void>
  },
): Promise<T> {
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms).unref()))
  let delay = WAIT_FIRST_MS
  let logged = false
  for (;;) {
    try {
      return await call()
    } catch (error) {
      if (!options.unreachable(error)) throw error
      if (!logged) options.log(`waiting for the backend: ${(error as Error).message}`)
      logged = true
      await sleep(delay)
      delay = Math.min(delay * 2, WAIT_MAX_MS)
    }
  }
}
